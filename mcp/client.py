"""MultiServerMCPClient — connect many MCP servers, expose their tools (P7).

Mirrors DeerFlow's ``MultiServerMCPClient``: hold a set of
:class:`~mcp.base.MCPServerConfig`, open a :class:`~mcp.base.MCPSession` per
server on demand, discover each server's tools, and render them as
:class:`tools.base.Tool` objects the lead agent can hand straight to the LLM.

Config shape (see ``mcp.example.yaml``)::

    servers:
      filesystem:
        transport: stdio
        command: npx
        args: ["-y", "@modelcontextprotocol/server-filesystem", "/workspace"]
      github:
        transport: http
        url: https://mcp.example.com/github
        headers:
          Authorization: Bearer ${GITHUB_MCP_TOKEN}
      docs:
        transport: sse
        url: https://mcp.example.com/docs/sse
        enabled: false

Design notes, consistent with the earlier phases:

- **Reflection-free but session-swappable.** Transports are chosen from the
  ``transport`` field by :func:`mcp.transports.create_session`; tests inject a
  fake ``session_factory`` so the whole suite stays offline.
- **Tool caching with invalidation.** The roadmap calls for a tool cache with
  "mtime invalidation". Discovered tools are cached per server; the cache is
  rebuilt when (a) the config file's mtime changes (a server was added / edited)
  or (b) :meth:`reload` is called explicitly — the roadmap's "运行时热加载".
- **Namespacing.** Each tool is exposed as ``<server>__<tool>`` so two servers
  can expose same-named tools without colliding in the flat registry.
- **Lazy + resilient.** A server is only contacted the first time its tools are
  needed, and a server that fails to connect degrades to *zero* tools (with the
  error captured) instead of breaking discovery of the healthy servers.
- **Process-level accessor.** :func:`mcp.get_mcp_client` / ``set_mcp_client``
  mirror the thread-store / sandbox / model-factory singletons so the tool
  layer, server and CLI share one client (and one connection pool).
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Callable

from mcp.base import (
    MCPError,
    MCPServerConfig,
    MCPSession,
    MCPToolSpec,
    MCPTransport,
)
from mcp.transports import create_session
from tools.base import Tool

# Type of the pluggable session factory (default: mcp.transports.create_session).
SessionFactory = Callable[[MCPServerConfig], MCPSession]


class MCPConfigError(MCPError):
    """Raised when ``mcp.yaml`` is malformed or references an unknown transport."""


# ---------------------------------------------------------------------------
# config loading
# ---------------------------------------------------------------------------
def _expand_env(value: Any) -> Any:
    """Expand ``${VAR}`` / ``$VAR`` references in strings using ``os.environ``.

    Lets ``mcp.yaml`` reference secrets (auth tokens) by env var instead of
    hard-coding them — ``Authorization: Bearer ${GITHUB_MCP_TOKEN}``. Applied
    recursively to strings inside lists / dicts; non-strings pass through.
    """
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    return value


def _parse_server(name: str, spec: dict[str, Any]) -> MCPServerConfig:
    """Turn one ``servers.<name>`` mapping into an :class:`MCPServerConfig`."""
    if not isinstance(spec, dict):
        raise MCPConfigError(
            f"MCP server {name!r} config must be a mapping, got {type(spec).__name__}"
        )
    spec = _expand_env(spec)
    transport = MCPTransport.coerce(spec.get("transport", "stdio"))
    args = spec.get("args") or []
    if not isinstance(args, list):
        raise MCPConfigError(f"MCP server {name!r}: 'args' must be a list")
    env = spec.get("env") or {}
    headers = spec.get("headers") or {}
    if not isinstance(env, dict) or not isinstance(headers, dict):
        raise MCPConfigError(f"MCP server {name!r}: 'env'/'headers' must be mappings")
    config = MCPServerConfig(
        name=name,
        transport=transport,
        command=spec.get("command", "") or "",
        args=tuple(str(a) for a in args),
        env={str(k): str(v) for k, v in env.items()},
        url=spec.get("url", "") or "",
        headers={str(k): str(v) for k, v in headers.items()},
        enabled=bool(spec.get("enabled", True)),
        tool_prefix=spec.get("tool_prefix"),
        timeout=float(spec.get("timeout", 30.0)),
    )
    config.validate()
    return config


def parse_servers(config: dict[str, Any] | None) -> list[MCPServerConfig]:
    """Parse the top-level ``servers:`` mapping into configs.

    A ``None`` / empty config yields an empty list — the "no servers" default
    that keeps MCP fully inert.
    """
    if not config:
        return []
    servers = config.get("servers")
    if servers is None:
        return []
    if not isinstance(servers, dict):
        raise MCPConfigError("'servers' must be a mapping of name -> server config")
    return [_parse_server(name, spec) for name, spec in servers.items()]


def _default_config_path() -> Path:
    """Where the client looks for ``mcp.yaml`` (overridable via ``MCP_CONFIG``)."""
    override = os.environ.get("MCP_CONFIG")
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parent.parent / "mcp.yaml"


def load_config(path: str | os.PathLike | None = None) -> dict[str, Any] | None:
    """Read ``mcp.yaml`` if it exists. Returns ``None`` for a missing file.

    Reuses PyYAML when present (the config is a small mapping) and falls back to
    the tiny loader in :mod:`models.factory` for environments without PyYAML —
    exactly mirroring how the P6 model factory loads its config, so MCP adds no
    new dependency.
    """
    target = Path(path) if path is not None else _default_config_path()
    if not target.exists():
        return None
    text = target.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore

        return yaml.safe_load(text) or {}
    except ImportError:  # pragma: no cover - PyYAML is in requirements.txt
        from models.factory import _tiny_yaml_load

        return _tiny_yaml_load(text)


# ---------------------------------------------------------------------------
# client
# ---------------------------------------------------------------------------
class MultiServerMCPClient:
    """Manage sessions to many MCP servers and surface their tools.

    The client holds the *configs*; sessions are opened lazily on first tool
    discovery and reused. Discovered :class:`~tools.base.Tool` objects are cached
    and only rebuilt when the config file's mtime changes or :meth:`reload` is
    called — the roadmap's "工具缓存(mtime 失效)" + "运行时热加载".
    """

    def __init__(
        self,
        servers: list[MCPServerConfig] | None = None,
        *,
        session_factory: SessionFactory | None = None,
        config_path: str | os.PathLike | None = None,
    ) -> None:
        self._servers: dict[str, MCPServerConfig] = {}
        for cfg in servers or []:
            self._servers[cfg.name] = cfg
        self._session_factory: SessionFactory = session_factory or create_session
        self._config_path = Path(config_path) if config_path is not None else None

        self._sessions: dict[str, MCPSession] = {}
        self._tool_cache: list[Tool] | None = None
        self._cache_mtime: float | None = None
        # Per-server discovery errors captured during the last build (for
        # introspection / logging without breaking healthy servers).
        self.errors: dict[str, str] = {}
        self._lock = threading.RLock()

    # -- introspection ------------------------------------------------------
    def has_servers(self) -> bool:
        """True iff at least one *enabled* server is configured."""
        return any(cfg.enabled for cfg in self._servers.values())

    def list_servers(self) -> list[str]:
        """Names of all configured servers (enabled or not)."""
        return list(self._servers.keys())

    def server_config(self, name: str) -> MCPServerConfig | None:
        return self._servers.get(name)

    # -- cache management ---------------------------------------------------
    def _config_mtime(self) -> float | None:
        """Current mtime of the backing config file, or None if not file-backed."""
        path = self._config_path or _default_config_path()
        try:
            return path.stat().st_mtime
        except OSError:
            return None

    def _cache_is_fresh(self) -> bool:
        """Whether the tool cache can be reused (config file unchanged)."""
        if self._tool_cache is None:
            return False
        return self._config_mtime() == self._cache_mtime

    def reload(self) -> None:
        """Drop cached tools and sessions; the next call rebuilds from scratch.

        This is the runtime hot-reload hook: after editing ``mcp.yaml`` (or
        adding a server via :meth:`add_server`) a caller invokes ``reload()`` and
        the fresh server set takes effect on the next :meth:`get_tools`.
        """
        with self._lock:
            self._close_sessions_locked()
            self._tool_cache = None
            self._cache_mtime = None
            self.errors = {}

    def add_server(self, config: MCPServerConfig) -> None:
        """Register (or replace) a server at runtime and invalidate the cache."""
        config.validate()
        with self._lock:
            self._servers[config.name] = config
            # Drop that server's live session so it reconnects with new config.
            old = self._sessions.pop(config.name, None)
            if old is not None:
                _safe_close(old)
            self._tool_cache = None

    # -- session lifecycle --------------------------------------------------
    def _get_session_locked(self, config: MCPServerConfig) -> MCPSession:
        session = self._sessions.get(config.name)
        if session is None:
            session = self._session_factory(config)
            self._sessions[config.name] = session
        return session

    def _close_sessions_locked(self) -> None:
        for session in self._sessions.values():
            _safe_close(session)
        self._sessions.clear()

    def close(self) -> None:
        """Close every open session. Safe to call multiple times."""
        with self._lock:
            self._close_sessions_locked()

    # -- tool discovery -----------------------------------------------------
    def get_tools(self, force: bool = False) -> list[Tool]:
        """Return the MCP tools across all enabled servers as :class:`Tool`s.

        Cached; the cache is transparently rebuilt when the config file's mtime
        changes. Pass ``force=True`` to rebuild unconditionally (equivalent to
        :meth:`reload` followed by a build).
        """
        with self._lock:
            if force:
                self._tool_cache = None
            if self._cache_is_fresh():
                return list(self._tool_cache or [])
            tools = self._build_tools_locked()
            self._tool_cache = tools
            self._cache_mtime = self._config_mtime()
            return list(tools)

    def _build_tools_locked(self) -> list[Tool]:
        """(Re)discover tools from every enabled server."""
        self.errors = {}
        built: list[Tool] = []
        for name, config in self._servers.items():
            if not config.enabled:
                continue
            try:
                session = self._get_session_locked(config)
                specs = session.list_tools()
            except MCPError as e:
                # A broken server must not sink the healthy ones — record and skip.
                self.errors[name] = str(e)
                continue
            for spec in specs:
                built.append(self._make_tool(config, session, spec))
        return built

    def _make_tool(
        self, config: MCPServerConfig, session: MCPSession, spec: MCPToolSpec
    ) -> Tool:
        """Wrap one discovered :class:`MCPToolSpec` as a callable :class:`Tool`.

        The tool's ``func`` closes over the live session and the *raw* tool name
        and forwards keyword arguments straight to ``session.call_tool``. Errors
        are turned into ``[mcp-error] ...`` text (mirroring the sandbox tools'
        graceful degradation) so the ReAct loop can read and recover from them.
        """
        namespaced = config.namespace(spec.name)
        raw_name = spec.name
        description = spec.description or f"MCP tool '{raw_name}' on server '{config.name}'."
        parameters = spec.input_schema or {"type": "object", "properties": {}}

        def _call(**kwargs: Any) -> str:
            try:
                return session.call_tool(raw_name, kwargs)
            except MCPError as e:
                return f"[mcp-error] {e}"

        return Tool(
            name=namespaced,
            description=description,
            parameters=parameters,
            func=_call,
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _safe_close(session: MCPSession) -> None:
    """Close a session, swallowing teardown errors (best-effort cleanup)."""
    try:
        session.close()
    except Exception:  # pragma: no cover - defensive
        pass


__all__ = [
    "MultiServerMCPClient",
    "MCPConfigError",
    "SessionFactory",
    "parse_servers",
    "load_config",
]
