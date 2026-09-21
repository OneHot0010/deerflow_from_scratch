"""Shared pytest fixtures & fakes for the mini-deerflow test suite.

Everything here is offline: no real network / Ark API call is ever made. We
fake the two shapes the code depends on from the Ark SDK:

- a *blocking* assistant message (``chat()`` return value): an object with
  ``.content`` and optional ``.tool_calls`` (each having ``.id`` and
  ``.function.name`` / ``.function.arguments``).
- a *streaming* completion chunk (``stream_chat()`` yields these): an object
  exposing ``.choices[0].delta`` where the delta carries incremental
  ``.content`` and/or ``.tool_calls`` (tool-call deltas expose ``.index``,
  ``.id`` and ``.function.name`` / ``.function.arguments``).

The project is a flat package (config.py / llm.py / tools/ / agents/ / server.py)
run from the repo root, so we make sure that root is importable.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

# --- make the repo root importable regardless of CWD ------------------------
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# --- fakes mirroring the Ark SDK object shapes ------------------------------
@dataclass
class FakeFunction:
    name: str
    arguments: str


@dataclass
class FakeToolCall:
    id: str
    function: FakeFunction
    type: str = "function"


@dataclass
class FakeMessage:
    """Stand-in for chat().return -> completion.choices[0].message."""

    content: str | None = ""
    tool_calls: list[FakeToolCall] | None = None


def make_message(content: str = "", tool_calls: list[dict] | None = None) -> FakeMessage:
    """Build a fake blocking assistant message.

    ``tool_calls`` items are dicts: {"id":..., "name":..., "arguments": <str>}.
    """
    calls = None
    if tool_calls:
        calls = [
            FakeToolCall(
                id=tc["id"],
                function=FakeFunction(name=tc["name"], arguments=tc["arguments"]),
            )
            for tc in tool_calls
        ]
    return FakeMessage(content=content, tool_calls=calls)


# streaming shapes -----------------------------------------------------------
@dataclass
class FakeDeltaToolCall:
    index: int
    id: str | None = None
    function: FakeFunction | None = None
    type: str = "function"


@dataclass
class FakeDelta:
    content: str | None = None
    tool_calls: list[FakeDeltaToolCall] | None = None


@dataclass
class FakeChoice:
    delta: FakeDelta


@dataclass
class FakeChunk:
    choices: list[FakeChoice]


def text_chunk(text: str) -> FakeChunk:
    """A streaming chunk carrying a piece of assistant text."""
    return FakeChunk(choices=[FakeChoice(delta=FakeDelta(content=text))])


def tool_chunk(
    index: int = 0,
    call_id: str | None = None,
    name: str | None = None,
    arguments: str | None = None,
) -> FakeChunk:
    """A streaming chunk carrying a tool-call delta fragment."""
    fn = None
    if name is not None or arguments is not None:
        fn = FakeFunction(name=name or "", arguments=arguments or "")
    tc = FakeDeltaToolCall(index=index, id=call_id, function=fn)
    return FakeChunk(choices=[FakeChoice(delta=FakeDelta(tool_calls=[tc]))])


def empty_chunk() -> FakeChunk:
    """A chunk with no choices (the loop must skip these gracefully)."""
    return FakeChunk(choices=[])


# --- fixtures ---------------------------------------------------------------
@pytest.fixture
def scripted_chat(monkeypatch):
    """Patch ``llm.chat`` to return a scripted queue of FakeMessages.

    Usage:
        turns = scripted_chat([make_message("hi")])
    Returns the recorded call-kwargs list so tests can assert what was sent.
    """
    import llm

    def _install(messages: list[FakeMessage]):
        queue = list(messages)
        recorded: list[dict] = []

        def fake_chat(msgs, model=None, temperature=0.7, tools=None, tool_choice=None, **extra):
            recorded.append(
                {"messages": msgs, "tools": tools, "tool_choice": tool_choice, "extra": extra}
            )
            return queue.pop(0) if queue else make_message("")

        monkeypatch.setattr(llm, "chat", fake_chat)
        return recorded

    return _install


@pytest.fixture
def scripted_stream(monkeypatch):
    """Patch ``llm.stream_chat`` to yield scripted lists of chunks per call.

    Usage:
        recorded = scripted_stream([[text_chunk("hi")], [text_chunk("bye")]])
    Each element is the chunk list for the Nth call to stream_chat.
    """
    import llm

    def _install(turns: list[list[Any]]):
        queue = list(turns)
        recorded: list[dict] = []

        def fake_stream(msgs, model=None, temperature=0.7, tools=None, tool_choice=None, **extra):
            recorded.append(
                {"messages": list(msgs), "tools": tools, "tool_choice": tool_choice, "extra": extra}
            )
            chunks = queue.pop(0) if queue else []
            for ch in chunks:
                yield ch

        monkeypatch.setattr(llm, "stream_chat", fake_stream)
        return recorded

    return _install


# --- P3: isolate the thread store per test (in-memory, offline) -------------
@pytest.fixture(autouse=True)
def _isolated_thread_store():
    """Give every test its own transient in-memory ThreadStore.

    Keeps the suite offline and side-effect free: no real ``threads.db`` file is
    ever created, and threads never leak between tests. Tests that want to drive
    the store directly can still ``import store`` and call ``store.get_store()``.
    """
    import store as _store

    fresh = _store.ThreadStore(":memory:")
    _store.set_store(fresh)
    try:
        yield fresh
    finally:
        fresh.close()
        _store.set_store(None)


# --- P6: reset the process-level ModelFactory between tests -----------------
@pytest.fixture(autouse=True)
def _isolated_model_factory():
    """Clear the cached factory so each test rebuilds it from a clean slate.

    Without this, one test's ``set_factory(...)`` (or a first-call load of
    ``models.yaml``) would leak into later tests and hide regressions. Kept
    autouse so every test — including the older P0-P5 ones — starts with
    ``get_factory()`` freshly resolving whatever env / config the test controls.
    Also resets the Ark provider's client-factory hook and the reflection
    cache so nothing survives across test boundaries.
    """
    import models as _models
    from models import reflection as _reflection
    from models.providers import ark as _ark

    _models.set_factory(None)
    _reflection.clear_cache()
    _ark.set_client_factory(None)
    try:
        yield
    finally:
        _models.set_factory(None)
        _reflection.clear_cache()
        _ark.set_client_factory(None)


# --- P7: reset the process-level MCP client between tests -------------------
@pytest.fixture(autouse=True)
def _isolated_mcp_client():
    """Clear the cached MCP client so each test starts fully inert.

    Without this, one test's ``set_mcp_client(...)`` (or a first-call build from
    ``mcp.yaml``) would leak into later tests — and a real singleton could try
    to spawn a subprocess / open a socket. Autouse so every test — including the
    older P0-P6 ones — begins with no MCP client installed (``get_mcp_client()``
    then rebuilds from whatever config the test controls, which is "no servers"
    by default, keeping MCP contributing zero tools).
    """
    import mcp as _mcp

    _mcp.set_mcp_client(None)
    try:
        yield
    finally:
        _mcp.set_mcp_client(None)
