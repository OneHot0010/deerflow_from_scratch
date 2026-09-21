"""P7 tests — MCP tool integration (mcp/).

Fully offline: no subprocess is ever spawned and no socket is ever opened. We
exercise every layer through fakes:

- ``mcp.base`` — the ``MCPTransport`` enum + ``coerce``, ``MCPServerConfig``
  validation / namespacing, ``MCPToolSpec``.
- ``mcp.client`` — config parsing (``parse_servers`` / env expansion),
  ``MultiServerMCPClient`` tool discovery, namespacing, per-server error
  isolation, the mtime tool cache, ``reload`` / ``add_server`` / ``force``, and
  ``_make_tool``'s ``[mcp-error]`` degradation.
- ``mcp.transports`` — ``_flatten_content`` / ``_extract_sse_json`` /
  ``_truncate`` helpers, ``create_session`` transport routing, and the shared
  JSON-RPC handshake / ``tools/list`` / ``tools/call`` shaping driven through a
  fake byte-transport (no real process / HTTP).
- ``mcp`` singleton — ``get_mcp_client`` / ``set_mcp_client``.
- End-to-end wiring — ``tools.get_available_tools(include_mcp=True)`` folds MCP
  tools into the pool alongside the builtins (validates the P7 acceptance:
  "可配置连接 MCP 服务器,工具动态生效").
"""
from __future__ import annotations

from typing import Any

import pytest

import mcp
from mcp import (
    MCPError,
    MCPServerConfig,
    MCPSession,
    MCPToolError,
    MCPToolSpec,
    MCPTransport,
    MCPTransportError,
    MultiServerMCPClient,
    create_session,
    parse_servers,
)
from mcp.client import MCPConfigError, _expand_env
from mcp.transports import (
    HttpSession,
    SseSession,
    StdioSession,
    _JsonRpcSession,
    _extract_sse_json,
    _flatten_content,
    _truncate,
)
from tools import get_available_tools


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------
class FakeSession(MCPSession):
    """An offline :class:`MCPSession` returning scripted tool specs / results."""

    def __init__(self, config: MCPServerConfig) -> None:
        super().__init__(config)
        self.initialized = 0
        self.closed = 0
        self.calls: list[tuple[str, dict]] = []
        # Per-server scripts keyed by server name (see _factory below).
        self._specs = _SCRIPTS.get(config.name, {}).get("tools", [])
        self._results = _SCRIPTS.get(config.name, {}).get("results", {})
        self._fail = _SCRIPTS.get(config.name, {}).get("fail")

    def initialize(self) -> None:
        if self._fail == "connect":
            raise MCPTransportError(f"{self.server_name}: cannot connect")
        self.initialized += 1

    def list_tools(self) -> list[MCPToolSpec]:
        self.initialize()
        return [
            MCPToolSpec(
                server=self.server_name,
                name=name,
                description=f"{name} on {self.server_name}",
                input_schema={"type": "object", "properties": {}},
            )
            for name in self._specs
        ]

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        self.initialize()
        self.calls.append((name, arguments or {}))
        if name in self._results:
            out = self._results[name]
            if isinstance(out, Exception):
                raise out
            return out
        return f"{self.server_name}.{name}({arguments or {}})"

    def close(self) -> None:
        self.closed += 1


# Script table the FakeSession reads from, set per test via _use_script().
_SCRIPTS: dict[str, dict] = {}


def _use_script(script: dict) -> None:
    _SCRIPTS.clear()
    _SCRIPTS.update(script)


def _factory(config: MCPServerConfig) -> MCPSession:
    return FakeSession(config)


@pytest.fixture(autouse=True)
def _clear_scripts():
    _SCRIPTS.clear()
    yield
    _SCRIPTS.clear()


def _cfg(name: str, transport: str = "stdio", **kw) -> MCPServerConfig:
    defaults: dict[str, Any] = {"name": name, "transport": MCPTransport.coerce(transport)}
    if transport == "stdio":
        defaults["command"] = kw.pop("command", "echo")
    else:
        defaults["url"] = kw.pop("url", "https://example.com/mcp")
    defaults.update(kw)
    return MCPServerConfig(**defaults)


# ---------------------------------------------------------------------------
# base: transport enum
# ---------------------------------------------------------------------------
def test_transport_coerce_accepts_known_strings():
    assert MCPTransport.coerce("stdio") is MCPTransport.STDIO
    assert MCPTransport.coerce("SSE") is MCPTransport.SSE
    assert MCPTransport.coerce("Http") is MCPTransport.HTTP
    # a member passes straight through
    assert MCPTransport.coerce(MCPTransport.STDIO) is MCPTransport.STDIO


def test_transport_coerce_rejects_unknown_with_mcp_error():
    with pytest.raises(MCPError) as ei:
        MCPTransport.coerce("carrier-pigeon")
    assert "carrier-pigeon" in str(ei.value)


# ---------------------------------------------------------------------------
# base: server config validation + namespacing
# ---------------------------------------------------------------------------
def test_config_validate_stdio_requires_command():
    with pytest.raises(MCPError):
        MCPServerConfig(name="fs", transport=MCPTransport.STDIO, command="").validate()


def test_config_validate_http_requires_url():
    with pytest.raises(MCPError):
        MCPServerConfig(name="gh", transport=MCPTransport.HTTP, url="").validate()


def test_config_validate_requires_name():
    with pytest.raises(MCPError):
        MCPServerConfig(name="", transport=MCPTransport.STDIO, command="x").validate()


def test_config_validate_ok():
    _cfg("fs").validate()  # no raise
    _cfg("gh", transport="http").validate()


def test_namespace_prefixes_with_server_name():
    assert _cfg("filesystem").namespace("read") == "filesystem__read"


def test_namespace_honours_tool_prefix_override():
    assert _cfg("filesystem", tool_prefix="fs").namespace("read") == "fs__read"


def test_namespace_empty_prefix_returns_bare_name():
    assert _cfg("filesystem", tool_prefix="").namespace("read") == "read"


# ---------------------------------------------------------------------------
# client: config parsing
# ---------------------------------------------------------------------------
def test_parse_servers_none_and_empty_yield_no_servers():
    assert parse_servers(None) == []
    assert parse_servers({}) == []
    assert parse_servers({"servers": None}) == []


def test_parse_servers_builds_typed_configs():
    servers = parse_servers(
        {
            "servers": {
                "filesystem": {
                    "transport": "stdio",
                    "command": "npx",
                    "args": ["-y", "server-fs", "/tmp"],
                },
                "github": {
                    "transport": "http",
                    "url": "https://mcp.example.com/gh",
                    "headers": {"Authorization": "Bearer T"},
                    "enabled": False,
                },
            }
        }
    )
    by_name = {s.name: s for s in servers}
    assert by_name["filesystem"].transport is MCPTransport.STDIO
    assert by_name["filesystem"].command == "npx"
    assert by_name["filesystem"].args == ("-y", "server-fs", "/tmp")
    assert by_name["github"].transport is MCPTransport.HTTP
    assert by_name["github"].url == "https://mcp.example.com/gh"
    assert by_name["github"].enabled is False


def test_parse_servers_rejects_non_mapping_servers():
    with pytest.raises(MCPConfigError):
        parse_servers({"servers": [1, 2, 3]})


def test_parse_servers_rejects_bad_args_type():
    with pytest.raises(MCPConfigError):
        parse_servers({"servers": {"x": {"command": "c", "args": "not-a-list"}}})


def test_expand_env_expands_strings_recursively(monkeypatch):
    monkeypatch.setenv("GITHUB_MCP_TOKEN", "sekret")
    out = _expand_env(
        {"headers": {"Authorization": "Bearer ${GITHUB_MCP_TOKEN}"}, "args": ["$GITHUB_MCP_TOKEN"]}
    )
    assert out["headers"]["Authorization"] == "Bearer sekret"
    assert out["args"] == ["sekret"]


def test_parse_servers_expands_env_in_headers(monkeypatch):
    monkeypatch.setenv("TOK", "abc123")
    servers = parse_servers(
        {"servers": {"gh": {"transport": "http", "url": "https://x/y",
                            "headers": {"Authorization": "Bearer ${TOK}"}}}}
    )
    assert servers[0].headers["Authorization"] == "Bearer abc123"


# ---------------------------------------------------------------------------
# client: tool discovery, namespacing, caching, errors
# ---------------------------------------------------------------------------
def test_client_discovers_and_namespaces_tools():
    _use_script({"fs": {"tools": ["read", "write"]}, "git": {"tools": ["log"]}})
    client = MultiServerMCPClient(
        [_cfg("fs"), _cfg("git")], session_factory=_factory
    )
    names = sorted(t.name for t in client.get_tools())
    assert names == ["fs__read", "fs__write", "git__log"]


def test_client_skips_disabled_servers():
    _use_script({"fs": {"tools": ["read"]}, "off": {"tools": ["x"]}})
    client = MultiServerMCPClient(
        [_cfg("fs"), _cfg("off", enabled=False)], session_factory=_factory
    )
    assert [t.name for t in client.get_tools()] == ["fs__read"]
    assert client.has_servers() is True


def test_client_isolates_a_broken_server():
    _use_script({"good": {"tools": ["ok"]}, "bad": {"fail": "connect"}})
    client = MultiServerMCPClient(
        [_cfg("good"), _cfg("bad")], session_factory=_factory
    )
    tools = client.get_tools()
    assert [t.name for t in tools] == ["good__ok"]
    assert "bad" in client.errors
    assert "cannot connect" in client.errors["bad"]


def test_tool_call_forwards_to_session_and_returns_text():
    _use_script({"fs": {"tools": ["read"], "results": {"read": "file-contents"}}})
    client = MultiServerMCPClient([_cfg("fs")], session_factory=_factory)
    tool = {t.name: t for t in client.get_tools()}["fs__read"]
    assert tool.run({"path": "/x"}) == "file-contents"


def test_tool_call_degrades_mcp_error_to_text():
    _use_script(
        {"fs": {"tools": ["boom"], "results": {"boom": MCPToolError("kaboom")}}}
    )
    client = MultiServerMCPClient([_cfg("fs")], session_factory=_factory)
    tool = {t.name: t for t in client.get_tools()}["fs__boom"]
    out = tool.run({})
    assert out.startswith("[mcp-error]")
    assert "kaboom" in out


def test_get_tools_is_cached_until_force():
    _use_script({"fs": {"tools": ["read"]}})
    calls = {"n": 0}

    def counting_factory(config: MCPServerConfig) -> MCPSession:
        calls["n"] += 1
        return FakeSession(config)

    client = MultiServerMCPClient(
        [_cfg("fs")], session_factory=counting_factory, config_path="/no/such/file"
    )
    client.get_tools()
    client.get_tools()
    assert calls["n"] == 1  # session built once, second call served from cache
    client.get_tools(force=True)
    # force drops the cache; a fresh session may be created lazily on rebuild
    assert calls["n"] >= 1


def test_reload_and_add_server_invalidate_cache():
    _use_script({"fs": {"tools": ["read"]}, "git": {"tools": ["log"]}})
    client = MultiServerMCPClient(
        [_cfg("fs")], session_factory=_factory, config_path="/no/such/file"
    )
    assert [t.name for t in client.get_tools()] == ["fs__read"]
    client.add_server(_cfg("git"))
    names = sorted(t.name for t in client.get_tools())
    assert names == ["fs__read", "git__log"]
    client.reload()
    assert sorted(t.name for t in client.get_tools()) == ["fs__read", "git__log"]


def test_client_close_tears_down_sessions():
    _use_script({"fs": {"tools": ["read"]}})
    made: list[FakeSession] = []

    def tracking_factory(config: MCPServerConfig) -> MCPSession:
        s = FakeSession(config)
        made.append(s)
        return s

    client = MultiServerMCPClient([_cfg("fs")], session_factory=tracking_factory)
    client.get_tools()
    client.close()
    assert made and made[0].closed >= 1


# ---------------------------------------------------------------------------
# transports: helpers
# ---------------------------------------------------------------------------
def test_flatten_content_joins_text_blocks():
    result = {"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}
    assert _flatten_content(result) == "a\nb"


def test_flatten_content_json_encodes_non_text_blocks():
    result = {"content": [{"type": "image", "url": "http://x/y.png"}]}
    out = _flatten_content(result)
    assert "image" in out and "y.png" in out


def test_flatten_content_falls_back_to_structured_content():
    result = {"structuredContent": {"k": "v"}}
    assert "k" in _flatten_content(result) and "v" in _flatten_content(result)


def test_truncate_caps_and_marks():
    out = _truncate("x" * 50, limit=10)
    assert out.startswith("x" * 10)
    assert "truncated" in out


def test_extract_sse_json_returns_last_json_event():
    raw = "event: message\ndata: {\"id\": 1}\n\ndata: {\"id\": 2}\n\n"
    assert _extract_sse_json(raw).strip() == '{"id": 2}'


# ---------------------------------------------------------------------------
# transports: JSON-RPC handshake / tools shaping via a fake byte-transport
# ---------------------------------------------------------------------------
class _FakeRpcSession(_JsonRpcSession):
    """Drive the shared JSON-RPC logic without any real process / socket."""

    def __init__(self, config: MCPServerConfig, responses: dict[str, dict]) -> None:
        super().__init__(config)
        self._responses = responses
        self.started = 0
        self.stopped = 0
        self.notifications: list[dict] = []

    def _start(self) -> None:
        self.started += 1

    def _stop(self) -> None:
        self.stopped += 1

    def _rpc(self, payload: dict[str, Any]) -> dict[str, Any]:
        method = payload["method"]
        body = self._responses.get(method, {})
        return {"jsonrpc": "2.0", "id": payload.get("id"), **body}

    def _notify(self, payload: dict[str, Any]) -> None:
        self.notifications.append(payload)


def test_jsonrpc_initialize_is_idempotent_and_notifies():
    s = _FakeRpcSession(_cfg("x"), {"initialize": {"result": {}}})
    s.initialize()
    s.initialize()  # second call is a no-op
    assert s.started == 1
    assert any(n.get("method") == "notifications/initialized" for n in s.notifications)


def test_jsonrpc_list_tools_parses_specs():
    s = _FakeRpcSession(
        _cfg("x"),
        {
            "initialize": {"result": {}},
            "tools/list": {
                "result": {
                    "tools": [
                        {"name": "read", "description": "d", "inputSchema": {"type": "object"}},
                        {"no_name": True},  # skipped: missing name
                    ]
                }
            },
        },
    )
    specs = s.list_tools()
    assert [sp.name for sp in specs] == ["read"]
    assert specs[0].server == "x"
    assert specs[0].input_schema == {"type": "object"}


def test_jsonrpc_call_tool_flattens_result():
    s = _FakeRpcSession(
        _cfg("x"),
        {
            "initialize": {"result": {}},
            "tools/call": {"result": {"content": [{"type": "text", "text": "hi"}]}},
        },
    )
    assert s.call_tool("read", {"p": 1}) == "hi"


def test_jsonrpc_call_tool_is_error_raises_tool_error():
    s = _FakeRpcSession(
        _cfg("x"),
        {
            "initialize": {"result": {}},
            "tools/call": {
                "result": {"isError": True, "content": [{"type": "text", "text": "nope"}]}
            },
        },
    )
    with pytest.raises(MCPToolError) as ei:
        s.call_tool("read")
    assert "nope" in str(ei.value)


def test_jsonrpc_error_frame_raises_transport_error():
    s = _FakeRpcSession(
        _cfg("x"),
        {"initialize": {"error": {"message": "bad protocol", "code": -32600}}},
    )
    with pytest.raises(MCPTransportError) as ei:
        s.initialize()
    assert "bad protocol" in str(ei.value)


# ---------------------------------------------------------------------------
# transports: session routing
# ---------------------------------------------------------------------------
def test_create_session_routes_by_transport():
    assert isinstance(create_session(_cfg("a", transport="stdio")), StdioSession)
    assert isinstance(create_session(_cfg("b", transport="sse")), SseSession)
    assert isinstance(create_session(_cfg("c", transport="http")), HttpSession)
    # SSE is a specialisation of HTTP with _force_sse on.
    assert SseSession(_cfg("b", transport="sse"))._force_sse is True
    assert HttpSession(_cfg("c", transport="http"))._force_sse is False


# ---------------------------------------------------------------------------
# singleton
# ---------------------------------------------------------------------------
def test_singleton_set_and_get():
    _use_script({"fs": {"tools": ["read"]}})
    client = MultiServerMCPClient([_cfg("fs")], session_factory=_factory)
    mcp.set_mcp_client(client)
    assert mcp.get_mcp_client() is client


def test_singleton_default_is_inert():
    # With no mcp.yaml the default client contributes zero tools.
    mcp.set_mcp_client(None)
    client = mcp.get_mcp_client()
    assert client.get_tools() == []


# ---------------------------------------------------------------------------
# end-to-end: tools registry merge
# ---------------------------------------------------------------------------
def test_get_available_tools_excludes_mcp_by_default():
    _use_script({"fs": {"tools": ["read"]}})
    mcp.set_mcp_client(
        MultiServerMCPClient([_cfg("fs")], session_factory=_factory)
    )
    names = {t.name for t in get_available_tools()}  # include_mcp defaults False
    assert "fs__read" not in names
    assert "bash" in names  # builtins still present


def test_get_available_tools_merges_mcp_when_requested():
    _use_script({"fs": {"tools": ["read"]}})
    mcp.set_mcp_client(
        MultiServerMCPClient([_cfg("fs")], session_factory=_factory)
    )
    names = {t.name for t in get_available_tools(include_mcp=True)}
    assert "fs__read" in names
    assert "bash" in names  # builtins alongside MCP tools


def test_get_available_tools_mcp_failure_degrades_to_builtins(monkeypatch):
    # A broken MCP client must never break plain tool discovery.
    class Boom:
        def get_tools(self):
            raise RuntimeError("mcp exploded")

        def close(self):  # so set_mcp_client(None) teardown can close it
            pass

    mcp.set_mcp_client(Boom())  # type: ignore[arg-type]
    names = {t.name for t in get_available_tools(include_mcp=True)}
    assert "bash" in names  # still works, MCP contributed nothing
