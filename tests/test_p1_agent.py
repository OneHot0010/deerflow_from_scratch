"""P1 tests: the blocking ReAct loop (LeadAgent.run).

We drive the loop deterministically by scripting ``llm.chat`` (via the
``scripted_chat`` fixture) so no model is called. A tiny in-memory tool lets us
assert the reason->act->observe->answer cycle without touching the shell.
"""
from __future__ import annotations

import pytest

from agents.lead_agent import LeadAgent
from tools.base import tool
from tests.conftest import make_message


@pytest.fixture
def counter_tool():
    """A recording tool: returns a fixed string and logs its calls."""
    calls = []

    @tool("ping", "returns pong", {"type": "object", "properties": {"n": {"type": "string"}}})
    def _ping(n: str = "") -> str:
        calls.append(n)
        return f"pong:{n}"

    _ping.calls = calls  # type: ignore[attr-defined]
    return _ping


def test_direct_answer_no_tools(scripted_chat):
    scripted_chat([make_message("final answer")])
    agent = LeadAgent(tools=[])
    assert agent.run("hello") == "final answer"


def test_single_tool_round_then_answer(scripted_chat, counter_tool):
    # Turn 1: model asks to call ping. Turn 2: model gives final text.
    recorded = scripted_chat(
        [
            make_message("", tool_calls=[{"id": "c1", "name": "ping", "arguments": '{"n": "1"}'}]),
            make_message("done using tool"),
        ]
    )
    agent = LeadAgent(tools=[counter_tool])
    out = agent.run("use the tool")

    assert out == "done using tool"
    assert counter_tool.calls == ["1"]  # tool actually invoked
    # Second LLM call must include the tool result as a `tool` message.
    second_call_msgs = recorded[1]["messages"]
    assert any(m.get("role") == "tool" and m.get("content") == "pong:1" for m in second_call_msgs)


def test_unknown_tool_is_reported_not_crash(scripted_chat):
    recorded = scripted_chat(
        [
            make_message("", tool_calls=[{"id": "c1", "name": "ghost", "arguments": "{}"}]),
            make_message("recovered"),
        ]
    )
    agent = LeadAgent(tools=[])
    out = agent.run("go")
    assert out == "recovered"
    tool_msgs = [m for m in recorded[1]["messages"] if m.get("role") == "tool"]
    assert tool_msgs and "unknown tool" in tool_msgs[0]["content"]


def test_max_steps_guard_forces_final(scripted_chat, counter_tool):
    # Model keeps asking for the tool forever; guard must stop it.
    loop_msg = make_message("", tool_calls=[{"id": "c", "name": "ping", "arguments": "{}"}])
    # Provide many tool-call turns + a final wrap-up call.
    scripted_chat([loop_msg] * 2 + [make_message("wrap up")])
    agent = LeadAgent(tools=[counter_tool], max_steps=2)
    out = agent.run("loop")
    assert out == "wrap up"


def test_on_event_hook_fires_for_tools(scripted_chat, counter_tool):
    scripted_chat(
        [
            make_message("", tool_calls=[{"id": "c1", "name": "ping", "arguments": "{}"}]),
            make_message("ok"),
        ]
    )
    events = []
    agent = LeadAgent(tools=[counter_tool], on_event=lambda k, p: events.append((k, p)))
    agent.run("go")
    kinds = [k for k, _ in events]
    assert "tool_start" in kinds and "tool_end" in kinds


def test_empty_question_raises(scripted_chat):
    scripted_chat([make_message("x")])
    agent = LeadAgent(tools=[])
    with pytest.raises(ValueError):
        agent.run("   ")


def test_tool_schemas_passed_to_llm(scripted_chat, counter_tool):
    recorded = scripted_chat([make_message("hi")])
    agent = LeadAgent(tools=[counter_tool])
    agent.run("q")
    sent_tools = recorded[0]["tools"]
    assert sent_tools and sent_tools[0]["function"]["name"] == "ping"
