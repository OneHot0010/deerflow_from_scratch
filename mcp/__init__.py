"""MCP package for mini-deerflow — P7 (MCP 工具集成).

Corresponds to DeerFlow's ``mcp/`` module: the layer that connects the agent to
**external MCP servers** (GitHub / filesystem / …) and folds the tools they
expose into the same registry the built-in tools live in — so the LLM can call a
remote MCP tool exactly like a local ``bash`` / ``read_file``.

Public surface (kept stable so later phases can grow it):

- ``MCPTransport`` / ``MCPServerConfig`` / ``MCPToolSpec`` — the config +
  discovery data types (``base.py``).
- ``MCPSession`` and the concrete ``StdioSession`` / ``SseSession`` /
  ``HttpSession`` transports (``base.py`` + ``transports.py``) — the three
  transports the roadmap's P7 calls for (stdio / SSE / HTTP).
- ``MultiServerMCPClient`` — connect many servers, discover + cache their tools,
  render them as :class:`tools.base.Tool` objects, with mtime-based cache
  invalidation and runtime hot-reload (``client.py``).
- ``MCPError`` / ``MCPTransportError`` / ``MCPToolError`` / ``MCPConfigError`` —
  typed failures.
- ``get_mcp_client()`` — the process-wide client singleton, mirroring
  ``sandbox.get_sandbox_provider()`` / ``models.get_factory()`` /
  ``store.get_store()`` in spirit: a single entry point the tool layer and the
  web / CLI call sites share.

The singleton is built lazily from ``mcp.yaml`` (or the file pointed at by the
``MCP_CONFIG`` env var) on first use. ``set_mcp_client()`` lets tests inject a
client backed by a fake session factory so the suite stays offline and never
spawns a subprocess or opens a socket, exactly like ``store.set_store()`` and
``sandbox.set_sandbox_provider()``.

**Off by default.** With no ``mcp.yaml`` (and ``config.MCP_ENABLED`` unset) the
client has zero servers and contributes zero tools, so P0-P6 behaviour — and
their offline test suites — are byte-for-byte unchanged.
"""
from __future__ import annotations

import threading

from mcp.base import (
    MCPError,
    MCPServerConfig,
    MCPSession,
    MCPToolError,
    MCPToolSpec,
    MCPTransport,
    MCPTransportError,
)
from mcp.client import (
    MCPConfigError,
    MultiServerMCPClient,
    SessionFactory,
    load_config,
    parse_servers,
)
from mcp.transports import (
    HttpSession,
    SseSession,
    StdioSession,
    create_session,
)

_client: MultiServerMCPClient | None = None
_lock = threading.Lock()


def get_mcp_client() -> MultiServerMCPClient:
    """Return the process-wide MCP client, creating it on first use.

    Lazily builds a :class:`MultiServerMCPClient` from ``mcp.yaml`` (or the file
    named by ``MCP_CONFIG``). The instance is cached so every caller shares one
    client — and therefore one pool of server sessions and one tool cache.

    A missing config file yields a client with no servers (fully inert), so this
    is always safe to call even when MCP is not in use.
    """
    global _client
    with _lock:
        if _client is None:
            config = load_config()
            _client = MultiServerMCPClient(parse_servers(config))
        return _client


def set_mcp_client(client: MultiServerMCPClient | None) -> None:
    """Install a custom client (or clear it with None).

    Used by tests to inject a client backed by a fake session factory so no real
    subprocess/socket is ever created. Passing None forces the next
    :func:`get_mcp_client` call to rebuild from config.
    """
    global _client
    with _lock:
        if _client is not None and client is not _client:
            _client.close()
        _client = client


__all__ = [
    # data types
    "MCPTransport",
    "MCPServerConfig",
    "MCPToolSpec",
    # sessions / transports
    "MCPSession",
    "StdioSession",
    "SseSession",
    "HttpSession",
    "create_session",
    # client
    "MultiServerMCPClient",
    "SessionFactory",
    "parse_servers",
    "load_config",
    # errors
    "MCPError",
    "MCPTransportError",
    "MCPToolError",
    "MCPConfigError",
    # singleton
    "get_mcp_client",
    "set_mcp_client",
]
