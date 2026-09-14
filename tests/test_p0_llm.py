"""P0 tests: the single LLM client wrapper (llm.py).

The wrapper is the one place that talks to the Ark SDK. We never hit the
network: we replace the shared client (``llm._client``) with a fake that
records the kwargs it was called with, then assert:

- chat() returns the raw assistant message and forwards tools/tool_choice.
- chat_completion() collapses to reply text (and tolerates None content).
- stream_chat() sets stream=True and yields the client's chunks through.
- embed() calls the multimodal-embeddings endpoint with the right args.
"""
from __future__ import annotations

import pytest

import llm
from tests.conftest import FakeMessage


class FakeCompletions:
    def __init__(self, to_return):
        self._to_return = to_return
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._to_return


class FakeChatNamespace:
    def __init__(self, completions):
        self.completions = completions


class FakeEmbeddings:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return {"data": [{"embedding": [0.1, 0.2]}]}


class FakeArk:
    def __init__(self, completion_return):
        self.chat = FakeChatNamespace(FakeCompletions(completion_return))
        self.multimodal_embeddings = FakeEmbeddings()


@pytest.fixture(autouse=True)
def _clear_client_cache():
    """llm._client is lru_cached; clear it around every test."""
    llm._client.cache_clear()
    yield
    llm._client.cache_clear()


def _patch_client(monkeypatch, fake):
    monkeypatch.setattr(llm, "_client", lambda: fake)


class _Completion:
    """chat.completions.create returns an object with .choices[0].message."""

    def __init__(self, message):
        self.choices = [type("C", (), {"message": message})()]


def test_chat_returns_message_and_forwards_tools(monkeypatch):
    msg = FakeMessage(content="hello")
    fake = FakeArk(_Completion(msg))
    _patch_client(monkeypatch, fake)

    tools = [{"type": "function", "function": {"name": "x", "parameters": {}}}]
    out = llm.chat([{"role": "user", "content": "hi"}], tools=tools)

    assert out is msg
    sent = fake.chat.completions.calls[0]
    assert sent["tools"] == tools
    assert sent["tool_choice"] == "auto"  # defaulted
    assert sent["model"] == llm.config.CHAT_MODEL


def test_chat_without_tools_omits_tool_keys(monkeypatch):
    fake = FakeArk(_Completion(FakeMessage(content="hi")))
    _patch_client(monkeypatch, fake)
    llm.chat([{"role": "user", "content": "hi"}])
    sent = fake.chat.completions.calls[0]
    assert "tools" not in sent
    assert "tool_choice" not in sent


def test_chat_respects_explicit_tool_choice(monkeypatch):
    fake = FakeArk(_Completion(FakeMessage(content="hi")))
    _patch_client(monkeypatch, fake)
    tools = [{"type": "function", "function": {"name": "x", "parameters": {}}}]
    llm.chat([{"role": "user", "content": "hi"}], tools=tools, tool_choice="none")
    assert fake.chat.completions.calls[0]["tool_choice"] == "none"


def test_chat_completion_returns_text(monkeypatch):
    fake = FakeArk(_Completion(FakeMessage(content="the answer")))
    _patch_client(monkeypatch, fake)
    assert llm.chat_completion([{"role": "user", "content": "q"}]) == "the answer"


def test_chat_completion_handles_none_content(monkeypatch):
    fake = FakeArk(_Completion(FakeMessage(content=None)))
    _patch_client(monkeypatch, fake)
    assert llm.chat_completion([{"role": "user", "content": "q"}]) == ""


def test_stream_chat_sets_stream_and_yields_chunks(monkeypatch):
    chunks = ["a", "b", "c"]
    fake = FakeArk(iter(chunks))  # create() returns an iterable of chunks
    _patch_client(monkeypatch, fake)

    out = list(llm.stream_chat([{"role": "user", "content": "hi"}]))
    assert out == chunks
    assert fake.chat.completions.calls[0]["stream"] is True


def test_stream_chat_forwards_tools(monkeypatch):
    fake = FakeArk(iter([]))
    _patch_client(monkeypatch, fake)
    tools = [{"type": "function", "function": {"name": "x", "parameters": {}}}]
    list(llm.stream_chat([{"role": "user", "content": "hi"}], tools=tools))
    sent = fake.chat.completions.calls[0]
    assert sent["tools"] == tools and sent["tool_choice"] == "auto"
    assert sent["stream"] is True


def test_embed_calls_multimodal_endpoint(monkeypatch):
    fake = FakeArk(_Completion(FakeMessage(content="")))
    _patch_client(monkeypatch, fake)
    inputs = [{"type": "text", "text": "hi"}]
    llm.embed(inputs)
    call = fake.multimodal_embeddings.calls[0]
    assert call["input"] == inputs
    assert call["model"] == llm.config.EMBEDDING_MODEL
    assert call["encoding_format"] == "float"
