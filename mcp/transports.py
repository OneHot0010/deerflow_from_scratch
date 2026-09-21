"""MCP transport sessions — P7 (MCP 工具集成).

Concrete :class:`~mcp.base.MCPSession` implementations that speak the MCP
JSON-RPC 2.0 protocol directly over stdlib, one per transport:

- :class:`StdioSession`  — spawns the server as a subprocess and exchanges
  newline-delimited JSON-RPC frames over its stdin/stdout. This is how local
  ``filesystem`` / ``git`` style servers run.
- :class:`HttpSession`   — POSTs JSON-RPC to a single "streamable HTTP"
  endpoint and reads the JSON (or ``text/event-stream``) response back.
- :class:`SseSession`    — the older HTTP + Server-Sent Events transport; here
  it is a thin specialisation of :class:`HttpSession` that always parses the
  response as an SSE stream.

All three share the JSON-RPC bookkeeping (monotonic request ids, the
``initialize`` -> ``notifications/initialized`` handshake, ``tools/list`` and
``tools/call`` shaping, MCP ``content`` block flattening) via
:class:`_JsonRpcSession`, so each concrete class only implements *how bytes
move* (``_send`` / ``_receive`` / ``_start`` / ``_stop``).

Zero new dependencies: ``subprocess`` for stdio, ``urllib.request`` for HTTP.
The official ``mcp`` SDK / ``langchain-mcp-adapters`` remain the aspirational
backends — a future ``McpSdkSession(MCPSession)`` slots in beside these without
touching the client or tool layer.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import urllib.error
import urllib.request
from typing import Any

from mcp.base import (
    MCPServerConfig,
    MCPSession,
    MCPToolError,
    MCPToolSpec,
    MCPTransport,
    MCPTransportError,
)

# The MCP protocol version this client advertises in the handshake. Servers
# negotiate down if they speak an older revision; we send a recent stable date.
PROTOCOL_VERSION = "2024-11-05"

# Identity we present in ``initialize.clientInfo`` so servers can log who called.
_CLIENT_INFO = {"name": "mini-deerflow", "version": "0.7.0"}

# Cap how much tool-result text we ever hand back to the model, mirroring the
# builtins' ``_MAX_OUTPUT_CHARS`` so a chatty MCP tool cannot blow the context.
_MAX_OUTPUT_CHARS = 20_000


def _truncate(text: str, limit: int = _MAX_OUTPUT_CHARS) -> str:
    """Hard-cap a string, appending a ``...[truncated N chars]`` marker."""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n...[truncated {len(text) - limit} chars]"


def _flatten_content(result: dict[str, Any]) -> str:
    """Flatten an MCP ``tools/call`` result into a single text string.

    An MCP result is ``{"content": [ {type, ...}, ... ], "isError": bool}``.
    We render each block to text (``text`` blocks pass through; other block
    kinds are JSON-encoded so nothing is silently dropped) and join them.
    """
    content = result.get("content")
    if content is None:
        # Some servers return a bare ``structuredContent`` / plain value.
        if "structuredContent" in result:
            return _truncate(
                json.dumps(result["structuredContent"], ensure_ascii=False, default=str)
            )
        return ""
    if not isinstance(content, list):
        return _truncate(str(content))

    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
        else:
            parts.append(json.dumps(block, ensure_ascii=False, default=str))
    return _truncate("\n".join(parts))


class _JsonRpcSession(MCPSession):
    """Shared JSON-RPC 2.0 bookkeeping for the concrete transports.

    Subclasses implement the byte movement:

    - ``_start()``  — bring the transport up (spawn process / no-op for HTTP).
    - ``_stop()``   — tear it down.
    - ``_rpc(payload)`` — send one JSON-RPC *request* dict and return the parsed
      response dict (or raise :class:`MCPTransportError`).
    - ``_notify(payload)`` — send one JSON-RPC *notification* (no response).

    This base owns request-id allocation, the initialize handshake, and the
    ``tools/list`` / ``tools/call`` protocol shaping.
    """

    def __init__(self, config: MCPServerConfig) -> None:
        super().__init__(config)
        self._id = 0
        self._initialized = False
        self._lock = threading.Lock()

    # -- id / payload helpers ----------------------------------------------
    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _request_payload(self, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
        }
        if params is not None:
            payload["params"] = params
        return payload

    # -- transport hooks (implemented by subclasses) -----------------------
    def _start(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def _stop(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def _rpc(self, payload: dict[str, Any]) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    def _notify(self, payload: dict[str, Any]) -> None:  # pragma: no cover
        raise NotImplementedError

    # -- MCPSession API -----------------------------------------------------
    def initialize(self) -> None:
        with self._lock:
            if self._initialized:
                return
            self._start()
            params = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": _CLIENT_INFO,
            }
            response = self._rpc(self._request_payload("initialize", params))
            self._check_error(response, "initialize")
            # Per the spec, tell the server we are ready before any real call.
            self._notify({"jsonrpc": "2.0", "method": "notifications/initialized"})
            self._initialized = True

    def list_tools(self) -> list[MCPToolSpec]:
        self.initialize()
        with self._lock:
            response = self._rpc(self._request_payload("tools/list", {}))
        self._check_error(response, "tools/list")
        tools_raw = (response.get("result") or {}).get("tools") or []
        specs: list[MCPToolSpec] = []
        for entry in tools_raw:
            if not isinstance(entry, dict) or "name" not in entry:
                continue
            specs.append(
                MCPToolSpec(
                    server=self.server_name,
                    name=entry["name"],
                    description=entry.get("description", "") or "",
                    input_schema=entry.get("inputSchema")
                    or entry.get("input_schema")
                    or {"type": "object", "properties": {}},
                )
            )
        return specs

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        self.initialize()
        params = {"name": name, "arguments": arguments or {}}
        with self._lock:
            response = self._rpc(self._request_payload("tools/call", params))
        self._check_error(response, f"tools/call {name!r}")
        result = response.get("result") or {}
        if result.get("isError"):
            raise MCPToolError(
                f"MCP tool {name!r} on server {self.server_name!r} failed: "
                f"{_flatten_content(result) or 'unknown error'}"
            )
        return _flatten_content(result)

    def close(self) -> None:
        with self._lock:
            self._initialized = False
            self._stop()

    # -- shared error handling ---------------------------------------------
    @staticmethod
    def _check_error(response: dict[str, Any], what: str) -> None:
        """Raise :class:`MCPTransportError` if a JSON-RPC ``error`` came back."""
        if isinstance(response, dict) and response.get("error"):
            err = response["error"]
            if isinstance(err, dict):
                msg = err.get("message", str(err))
                code = err.get("code")
                detail = f"{msg} (code {code})" if code is not None else msg
            else:
                detail = str(err)
            raise MCPTransportError(f"{what} returned an error: {detail}")


class StdioSession(_JsonRpcSession):
    """Talk JSON-RPC to a locally spawned MCP server over stdio.

    The server is launched with ``config.command`` + ``config.args`` and a
    merged environment (``os.environ`` overlaid with ``config.env``). Each
    JSON-RPC frame is a single line (``\\n``-terminated JSON) written to the
    child's stdin; responses are read line-by-line from its stdout. Anything the
    server writes to stderr is left to flow to the parent's stderr for debugging.
    """

    def __init__(self, config: MCPServerConfig) -> None:
        super().__init__(config)
        self._proc: subprocess.Popen[str] | None = None

    def _start(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        env = dict(os.environ)
        env.update(self._config.env)
        try:
            self._proc = subprocess.Popen(
                [self._config.command, *self._config.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=None,
                text=True,
                bufsize=1,  # line-buffered
                env=env,
            )
        except (OSError, ValueError) as e:
            raise MCPTransportError(
                f"MCP server {self.server_name!r}: could not spawn "
                f"{self._config.command!r}: {e}"
            ) from e

    def _stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _write_line(self, payload: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise MCPTransportError(
                f"MCP server {self.server_name!r}: stdio transport is not running"
            )
        try:
            self._proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
        except (OSError, ValueError) as e:
            raise MCPTransportError(
                f"MCP server {self.server_name!r}: failed writing to stdin: {e}"
            ) from e

    def _rpc(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._write_line(payload)
        want_id = payload.get("id")
        if self._proc is None or self._proc.stdout is None:  # pragma: no cover
            raise MCPTransportError(
                f"MCP server {self.server_name!r}: stdio transport is not running"
            )
        # Read lines until we see the response matching our request id, skipping
        # any interleaved notifications the server may emit.
        while True:
            line = self._proc.stdout.readline()
            if line == "":
                raise MCPTransportError(
                    f"MCP server {self.server_name!r}: connection closed while "
                    f"awaiting response to {payload.get('method')!r}"
                )
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                # Not a JSON-RPC frame (stray log line) — ignore and keep reading.
                continue
            if isinstance(message, dict) and message.get("id") == want_id:
                return message
            # A notification or an unrelated id: skip it.

    def _notify(self, payload: dict[str, Any]) -> None:
        self._write_line(payload)


class HttpSession(_JsonRpcSession):
    """Talk JSON-RPC to an MCP server over "streamable HTTP".

    Each request is a JSON-RPC POST to ``config.url``; the response body is
    parsed as JSON, or — when the server answers ``text/event-stream`` — as an
    SSE stream whose ``data:`` frames carry the JSON-RPC message. Notifications
    are POSTed the same way and their (empty / accepted) response is discarded.

    Auth tokens and similar travel via ``config.headers``.
    """

    #: When True the response is always parsed as SSE (used by :class:`SseSession`).
    _force_sse = False

    def _start(self) -> None:
        # HTTP is connectionless here — nothing to bring up. (A future keep-alive
        # pool would open here.)
        return

    def _stop(self) -> None:
        return

    def _post(self, payload: dict[str, Any]) -> str:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        headers.update(self._config.headers)
        req = urllib.request.Request(
            self._config.url, data=body, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=self._config.timeout) as resp:
                raw = resp.read().decode("utf-8")
                content_type = resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:  # pragma: no cover - network dependent
            raise MCPTransportError(
                f"MCP server {self.server_name!r}: HTTP {e.code} from {self._config.url}"
            ) from e
        except (urllib.error.URLError, OSError) as e:  # pragma: no cover
            raise MCPTransportError(
                f"MCP server {self.server_name!r}: cannot reach {self._config.url}: {e}"
            ) from e
        if self._force_sse or "text/event-stream" in content_type:
            return _extract_sse_json(raw)
        return raw

    def _rpc(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw = self._post(payload)
        if not raw.strip():
            raise MCPTransportError(
                f"MCP server {self.server_name!r}: empty response to "
                f"{payload.get('method')!r}"
            )
        try:
            message = json.loads(raw)
        except json.JSONDecodeError as e:
            raise MCPTransportError(
                f"MCP server {self.server_name!r}: malformed JSON response: {e}"
            ) from e
        if not isinstance(message, dict):
            raise MCPTransportError(
                f"MCP server {self.server_name!r}: unexpected response shape"
            )
        return message

    def _notify(self, payload: dict[str, Any]) -> None:
        # Fire-and-forget; the transport still needs the POST to happen but we do
        # not care about the (typically 202 Accepted / empty) body.
        try:
            self._post(payload)
        except MCPTransportError:
            # A server that rejects the initialized-notification POST should not
            # abort the whole session; the next real call will surface issues.
            pass


class SseSession(HttpSession):
    """MCP over HTTP + Server-Sent Events.

    Identical to :class:`HttpSession` except responses are always parsed as an
    ``text/event-stream`` (some SSE servers omit / mislabel the Content-Type).
    """

    _force_sse = True


def _extract_sse_json(raw: str) -> str:
    """Pull the JSON payload out of an SSE response body.

    An SSE stream is a sequence of ``field: value`` lines grouped into events by
    blank lines. We concatenate the ``data:`` lines of the *last* event that
    carries JSON — that is the JSON-RPC response frame.
    """
    events: list[str] = []
    current: list[str] = []
    for line in raw.splitlines():
        if line == "":
            if current:
                events.append("\n".join(current))
                current = []
            continue
        if line.startswith("data:"):
            current.append(line[len("data:"):].lstrip())
    if current:
        events.append("\n".join(current))
    # Return the last event that parses as a JSON object.
    for chunk in reversed(events):
        stripped = chunk.strip()
        if stripped:
            return stripped
    return raw


def create_session(config: MCPServerConfig) -> MCPSession:
    """Build the right :class:`MCPSession` for ``config.transport``.

    This is the default session factory :class:`~mcp.client.MultiServerMCPClient`
    uses; tests inject their own factory to stay offline.
    """
    config.validate()
    transport = MCPTransport.coerce(config.transport)
    if transport is MCPTransport.STDIO:
        return StdioSession(config)
    if transport is MCPTransport.SSE:
        return SseSession(config)
    return HttpSession(config)


__all__ = [
    "PROTOCOL_VERSION",
    "StdioSession",
    "HttpSession",
    "SseSession",
    "create_session",
]
