"""P2 tests: the streaming ReAct loop (LeadAgent.run_stream).

We script ``llm.stream_chat`` (via ``scripted_stream``) with lists of fake
chunks per model turn, then assert the *event* sequence the web layer relies
on: message_chunk / tool_start / tool_end / final / error / max_steps.

Key behaviours under test:
- plain streamed answer -> only message_chunk(s) then final.
- tool-call deltas split across chunks are reassembled and executed.
- empty chunks (no choices) are skipped without crashing.
- an empty question yields a single error event (no exception).
- an exception inside the loop is converted into an error event.
- exhausting max_steps emits a max_steps event then a final wrap-up.
"""
from __future__ import annotations

import pytest

from agents.lead_agent import LeadAgent
from tools.base import tool
from tests.conftest import text_chunk, tool_chunk, empty_chunk


@pytest.fixture
def echo_tool():
    calls = []

    @tool("shout", "uppercases input", {"type": "object", "properties": {"s": {"type": "string"}}})
    def _shout(s: str = "") -> str:
        calls.append(s)
        return s.upper()

    _shout.calls = calls  # type: ignore[attr-defined]
    return _shout


def _types(events):
    return [e["type"] for e in events]


def test_plain_answer_streams_then_final(scripted_stream):
    scripted_stream([[text_chunk("Hel"), text_chunk("lo")]])
    agent = LeadAgent(tools=[])
    events = list(agent.run_stream("hi"))

    assert _types(events) == ["message_chunk", "message_chunk", "final"]
    assert events[0]["delta"] == "Hel"
    assert events[-1]["content"] == "Hello"


def test_empty_chunks_are_skipped(scripted_stream):
    scripted_stream([[empty_chunk(), text_chunk("ok"), empty_chunk()]])
    agent = LeadAgent(tools=[])
    events = list(agent.run_stream("hi"))
    assert _types(events) == ["message_chunk", "final"]
    assert events[-1]["content"] == "ok"


def test_tool_call_reassembled_across_chunks(scripted_stream, echo_tool):
    # Turn 1: a tool call whose name + JSON arguments arrive in fragments.
    turn1 = [
        tool_chunk(index=0, call_id="c1", name="shout"),
        tool_chunk(index=0, arguments='{"s": "he'),
        tool_chunk(index=0, arguments='llo"}'),
    ]
    # Turn 2: the final textual answer.
    turn2 = [text_chunk("did it")]
    scripted_stream([turn1, turn2])

    agent = LeadAgent(tools=[echo_tool])
    events = list(agent.run_stream("shout hello"))

    assert _types(events) == ["tool_start", "tool_end", "message_chunk", "final"]
    assert events[0]["name"] == "shout"
    assert events[0]["arguments"] == '{"s": "hello"}'  # reassembled
    assert events[1]["result"] == "HELLO"
    assert echo_tool.calls == ["hello"]
    assert events[-1]["content"] == "did it"


def test_multiple_parallel_tool_calls(scripted_stream, echo_tool):
    turn1 = [
        tool_chunk(index=0, call_id="a", name="shout", arguments='{"s":"x"}'),
        tool_chunk(index=1, call_id="b", name="shout", arguments='{"s":"y"}'),
    ]
    scripted_stream([turn1, [text_chunk("both done")]])
    agent = LeadAgent(tools=[echo_tool])
    events = list(agent.run_stream("go"))
    starts = [e for e in events if e["type"] == "tool_start"]
    assert len(starts) == 2
    assert echo_tool.calls == ["x", "y"]


def test_unknown_tool_streams_error_result(scripted_stream):
    turn1 = [tool_chunk(index=0, call_id="c", name="ghost", arguments="{}")]
    scripted_stream([turn1, [text_chunk("recovered")]])
    agent = LeadAgent(tools=[])
    events = list(agent.run_stream("go"))
    tool_end = [e for e in events if e["type"] == "tool_end"][0]
    assert "unknown tool" in tool_end["result"]
    assert events[-1]["content"] == "recovered"


def test_empty_question_yields_error_event(scripted_stream):
    scripted_stream([])
    agent = LeadAgent(tools=[])
    events = list(agent.run_stream("   "))
    assert _types(events) == ["error"]
    assert "non-empty" in events[0]["message"]


def test_exception_becomes_error_event(monkeypatch):
    import llm

    def boom(*a, **k):
        raise RuntimeError("provider down")
        yield  # pragma: no cover  (make it a generator)

    monkeypatch.setattr(llm, "stream_chat", boom)
    agent = LeadAgent(tools=[])
    events = list(agent.run_stream("hi"))
    assert events[-1]["type"] == "error"
    assert "provider down" in events[-1]["message"]


def test_max_steps_emits_event_then_final(scripted_stream, echo_tool):
    # Every turn asks for a tool -> loop hits the step cap.
    loop_turn = [tool_chunk(index=0, call_id="c", name="shout", arguments='{"s":"a"}')]
    # 2 looping turns (max_steps=2) + 1 wrap-up stream call.
    scripted_stream([loop_turn, loop_turn, [text_chunk("stopped")]])
    agent = LeadAgent(tools=[echo_tool], max_steps=2)
    events = list(agent.run_stream("loop"))
    types = _types(events)
    assert "max_steps" in types
    assert events[-1]["type"] == "final"
    assert events[-1]["content"] == "stopped"


def test_on_event_hook_fires_in_stream(scripted_stream, echo_tool):
    scripted_stream(
        [[tool_chunk(index=0, call_id="c", name="shout", arguments='{"s":"z"}')], [text_chunk("ok")]]
    )
    seen = []
    agent = LeadAgent(tools=[echo_tool], on_event=lambda k, p: seen.append(k))
    list(agent.run_stream("go"))
    assert "tool_start" in seen and "tool_end" in seen
