"""MCP abstractions — P7 (MCP 工具集成).

Mirrors the two-layer contract at the heart of DeerFlow's ``mcp/`` module
(``MultiServerMCPClient`` + per-server transport sessions), reduced to the
smallest useful form in keeping with the earlier phases:

- ``MCPTransport``     — the enum of wire transports a server can speak:
  ``stdio`` (spawn a subprocess and talk JSON-RPC over its stdin/stdout),
  ``sse`` (Server-Sent Events over HTTP) and ``http`` (streamable HTTP). This
  is the roadmap's "stdio/SSE/HTTP three transports" requirement.
- ``MCPServerConfig``  — one server's declared connection (name + transport +
  the parameters that transport needs). The unit the client builds a session
  from, and the shape ``mcp.yaml`` parses into.
- ``MCPSession``       — a *live connection* to one server: ``initialize()``
  handshakes, ``list_tools()`` discovers the server's tools, ``call_tool()``
  invokes one, ``close()`` tears it down. This is the abstract transport
  session the concrete ``stdio``/``sse``/``http`` backends implement.
- ``MCPToolSpec``      — a discovered tool's metadata (name / description /
  input JSON schema), decoupled from any transport so the client can cache and
  render it into a :class:`tools.base.Tool` uniformly.
- ``MCPError`` / ``MCPTransportError`` / ``MCPToolError`` — typed failures so
  the tool layer can tell a connection problem apart from a tool-call error and
  render each as recoverable text for the ReAct loop.

Design constraints carried over from P0-P6:

- **Zero new dependencies.** Pure stdlib. ``langchain-mcp-adapters`` and the
  official ``mcp`` SDK are the *aspirational* backends named in the roadmap's
  "关键技术" column; the abstraction is shaped so an ``McpSdkSession`` can slot
  in behind :class:`MCPSession` without touching the client or the tool layer,
  but the only sessions we ship in P7 speak the MCP JSON-RPC protocol directly
  over stdlib ``subprocess`` / ``urllib``.
- **Off by default.** No server is contacted unless ``mcp.yaml`` declares one
  *and* ``config.MCP_ENABLED`` is set, so P0-P6 behaviour (and their offline
  tests) is byte-for-byte unchanged.
- **Sessions are the seam.** Everything above the transport talks to
  :class:`MCPSession`; swapping stdio for SSE — or the whole stack for the real
  MCP SDK — never touches ``MultiServerMCPClient`` or ``get_available_tools``.

The abstract methods intentionally track the subset of the MCP protocol the
mini-deerflow tool layer actually needs — ``initialize`` / ``list_tools`` /
``call_tool`` — leaving richer parts of the spec (resources / prompts /
sampling / notifications) to later work.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class MCPError(Exception):
    """Base class for MCP failures the tool layer can render as text."""


class MCPTransportError(MCPError):
    """A connection / protocol-level failure talking to an MCP server.

    Raised when a session cannot be started, the handshake fails, the transport
    drops, or a malformed JSON-RPC frame comes back. Distinct from
    :class:`MCPToolError` so a caller can tell "the server is unreachable" apart
    from "the tool ran but errored".
    """


class MCPToolError(MCPError):
    """A tool call reached the server but the server reported an error.

    Carries the server-provided message so the ReAct loop can read it and
    recover (retry with different arguments, pick another tool, …) instead of
    crashing.
    """


class MCPTransport(str, Enum):
    """The wire transports an MCP server can speak.

    A ``str`` enum so config values (``transport: stdio``) map straight onto a
    member and the value round-trips cleanly through YAML / JSON.
    """

    STDIO = "stdio"
    """Spawn a subprocess and exchange newline-delimited JSON-RPC over its
    stdin/stdout — the transport ``filesystem`` / ``git`` style servers use."""

    SSE = "sse"
    """HTTP + Server-Sent Events: POST requests, responses streamed back as
    ``text/event-stream`` frames."""

    HTTP = "http"
    """Streamable HTTP: plain request/response JSON-RPC over a single endpoint
    (the newer MCP transport that supersedes bare SSE)."""

    @classmethod
    def coerce(cls, value: "str | MCPTransport") -> "MCPTransport":
        """Return the matching member for a string (case-insensitive).

        Raises :class:`MCPError` on an unknown transport so a typo in
        ``mcp.yaml`` produces an actionable message instead of a ``ValueError``.
        """
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except ValueError as e:
            known = ", ".join(t.value for t in cls)
            raise MCPError(
                f"unknown MCP transport {value!r}; expected one of: {known}"
            ) from e


@dataclass(frozen=True)
class MCPServerConfig:
    """One MCP server's declared connection.

    Attributes:
        name:      Unique server name. Used to namespace its tools (so two
                   servers exposing a ``search`` tool don't collide) and to
                   look the server up for hot-reload.
        transport: Which :class:`MCPTransport` this server speaks.
        command:   (stdio) The executable to spawn, e.g. ``npx``.
        args:      (stdio) Arguments passed to ``command``.
        env:       (stdio) Extra environment variables for the subprocess,
                   merged over the parent environment.
        url:       (sse / http) The server endpoint URL.
        headers:   (sse / http) Extra HTTP headers (auth tokens, …).
        enabled:   Per-server off switch; a disabled server is skipped even
                   when MCP is globally on. Defaults to True.
        tool_prefix: Optional override for the namespace prefix applied to this
                   server's tool names. Defaults to ``name`` (see
                   :meth:`namespace`).
        timeout:   Per-request wall-clock timeout (seconds) for this server.

    The config is frozen so it can be cached and shared across threads. Its
    fields are the union across transports; :meth:`validate` enforces that the
    ones a given transport needs are present.
    """

    name: str
    transport: MCPTransport
    command: str = ""
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    tool_prefix: str | None = None
    timeout: float = 30.0

    def validate(self) -> None:
        """Raise :class:`MCPError` if the transport's required fields are missing."""
        if not self.name:
            raise MCPError("MCP server config is missing a 'name'")
        if self.transport is MCPTransport.STDIO:
            if not self.command:
                raise MCPError(
                    f"MCP server {self.name!r}: stdio transport requires 'command'"
                )
        else:  # SSE / HTTP
            if not self.url:
                raise MCPError(
                    f"MCP server {self.name!r}: {self.transport.value} transport "
                    "requires 'url'"
                )

    def namespace(self, tool_name: str) -> str:
        """Return ``tool_name`` namespaced by this server.

        Two servers may expose a ``read`` tool; namespacing by the server name
        (``filesystem__read`` / ``git__read``) keeps them distinct in the flat
        tool registry the LLM sees. A double underscore is used as the
        separator because it is legal in OpenAI/Ark function names and unlikely
        to appear inside a raw tool name.
        """
        prefix = self.tool_prefix if self.tool_prefix is not None else self.name
        if not prefix:
            return tool_name
        return f"{prefix}__{tool_name}"


@dataclass(frozen=True)
class MCPToolSpec:
    """Metadata for one tool discovered on an MCP server.

    Attributes:
        server:      The name of the server this tool lives on.
        name:        The tool's *raw* name as the server reports it.
        description: Human/LLM-readable description from the server.
        input_schema: JSON Schema (object) for the tool's arguments — fed
                     straight into :class:`tools.base.Tool.parameters`.

    Frozen so a discovered tool list can be cached without defensive copying.
    """

    server: str
    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)


class MCPSession(ABC):
    """A live connection to a single MCP server.

    One session owns one transport (a subprocess for stdio, an HTTP connection
    for sse/http). The lifecycle mirrors the MCP protocol:

        ``initialize()``   -> perform the protocol handshake (once)
        ``list_tools()``   -> discover the server's tools
        ``call_tool()``    -> invoke a tool by (raw) name with arguments
        ``close()``        -> tear the transport down

    Implementations translate these onto JSON-RPC requests over their wire.
    Everything above this class (``MultiServerMCPClient``, the tool layer) is
    transport-agnostic, so a future ``McpSdkSession`` delegating to the official
    SDK can replace a stdlib session without a single call-site change.
    """

    def __init__(self, config: MCPServerConfig) -> None:
        self._config = config

    @property
    def config(self) -> MCPServerConfig:
        """The :class:`MCPServerConfig` this session was built from."""
        return self._config

    @property
    def server_name(self) -> str:
        """Convenience accessor for the server's name."""
        return self._config.name

    @abstractmethod
    def initialize(self) -> None:
        """Perform the MCP ``initialize`` handshake.

        Idempotent: implementations must tolerate being called more than once
        (later calls are no-ops once the handshake succeeded).

        Raises:
            MCPTransportError: If the transport cannot be established or the
                handshake fails.
        """

    @abstractmethod
    def list_tools(self) -> list[MCPToolSpec]:
        """Return the tools this server exposes (via ``tools/list``).

        Raises:
            MCPTransportError: On a connection / protocol failure.
        """

    @abstractmethod
    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        """Invoke a tool by its *raw* name and return its result as text.

        Args:
            name: The tool's raw name (not namespaced).
            arguments: JSON-serialisable argument mapping.

        Returns:
            The tool's textual result, with the MCP content blocks flattened to
            a single string the ReAct loop can feed back as a ``tool`` message.

        Raises:
            MCPToolError: If the server reports the tool call as an error.
            MCPTransportError: On a connection / protocol failure.
        """

    @abstractmethod
    def close(self) -> None:
        """Tear the transport down. Safe to call multiple times."""


__all__ = [
    "MCPError",
    "MCPTransportError",
    "MCPToolError",
    "MCPTransport",
    "MCPServerConfig",
    "MCPToolSpec",
    "MCPSession",
]
