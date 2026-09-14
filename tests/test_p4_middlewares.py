"""P4 tests: the middleware chain (agents/middlewares/ + LeadAgent wiring).

Everything here is offline. We exercise:

- MiddlewareChain ordering: before_* run first→last, after_* run last→first,
  and a raising hook is isolated (recorded on ctx.scratch['_errors']).
- LeadAgent backward compatibility: with no factory the chain is empty, so the
  P0-P3 behaviour is untouched.
- LeadAgent lifecycle wiring: a probe middleware sees before_agent /
  before_model / after_model / after_agent fire the right number of times, in
  both the blocking (run) and streaming (run_stream) paths, and its emitted
  events are forwarded (to on_event and as SSE dicts respectively).
- TitleMiddleware: derives a title after the first assistant turn (via a faked
  llm.chat_completion), falls back on failure, and no-ops on a resumed thread.
- TodoListMiddleware: injects a write_todos tool, keeps a [todo-list] reminder
  in front of the model, and never leaves it in the persisted conversation.
- SummarizationMiddleware: triggers past the token budget, preserves the
  leading system prompt + the tail, and never orphans a tool result.
"""
from __future__ import annotations

import llm
from agents.lead_agent import LeadAgent
from agents.middlewares import (
    AgentContext,
    Middleware,
    MiddlewareChain,
    SummarizationMiddleware,
    TitleMiddleware,
    TodoListMiddleware,
    default_middlewares,
)
from tests.conftest import make_message, text_chunk, tool_chunk


# --- MiddlewareChain: ordering + isolation ----------------------------------
class _Recorder(Middleware):
    """Middleware that appends its label+hook to a shared trace list."""

    def __init__(self, label: str, trace: list[str]) -> None:
        self.name = label
        self._trace = trace

    def before_agent(self, ctx):
        self._trace.append(f"{self.name}:before_agent")

    def before_model(self, ctx):
        self._trace.append(f"{self.name}:before_model")

    def after_model(self, ctx):
        self._trace.append(f"{self.name}:after_model")

    def after_agent(self, ctx):
        self._trace.append(f"{self.name}:after_agent")


def _ctx() -> AgentContext:
    return AgentContext(messages=[{"role": "user", "content": "hi"}])


def test_chain_runs_before_hooks_in_order_after_hooks_reversed():
    trace: list[str] = []
    chain = MiddlewareChain([_Recorder("A", trace), _Recorder("B", trace)])
    ctx = _ctx()

    chain.before_agent(ctx)
    chain.before_model(ctx)
    chain.after_model(ctx)
    chain.after_agent(ctx)

    assert trace == [
        "A:before_agent",
        "B:before_agent",
        "A:before_model",
        "B:before_model",
        "B:after_model",  # after_* reversed → B before A
        "A:after_model",
        "B:after_agent",
        "A:after_agent",
    ]


def test_chain_len_and_registration_type_guard():
    chain = MiddlewareChain()
    assert len(chain) == 0
    chain.register(_Recorder("A", []))
    assert len(chain) == 1
    try:
        chain.register(object())  # type: ignore[arg-type]
    except TypeError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected TypeError for a non-Middleware")


def test_chain_isolates_a_raising_hook():
    class Boom(Middleware):
        name = "boom"

        def before_model(self, ctx):
            raise RuntimeError("kaboom")

    trace: list[str] = []
    chain = MiddlewareChain([Boom(), _Recorder("A", trace)])
    ctx = _ctx()
    chain.before_model(ctx)  # must not raise

    # The good middleware still ran, and the error was recorded, not propagated.
    assert trace == ["A:before_model"]
    errors = ctx.scratch.get("_errors", [])
    assert len(errors) == 1
    assert errors[0]["middleware"] == "boom"
    assert errors[0]["hook"] == "before_model"
    assert "RuntimeError" in errors[0]["error"]


# --- LeadAgent: backward compatibility (empty chain by default) -------------
def test_agent_without_factory_uses_empty_chain(scripted_chat):
    scripted_chat([make_message("plain answer")])
    agent = LeadAgent()  # no middleware_factory
    out = agent.run("hello")
    assert out == "plain answer"
    # No middleware ran → no derived outputs.
    assert agent.title is None
    assert agent.todos == []


# --- LeadAgent: lifecycle wiring (blocking path) ----------------------------
class _Probe(Middleware):
    """Counts hook fires and emits one event so we can assert forwarding."""

    name = "probe"

    def __init__(self) -> None:
        self.counts = {
            "before_agent": 0,
            "before_model": 0,
            "after_model": 0,
            "after_agent": 0,
        }

    def before_agent(self, ctx):
        self.counts["before_agent"] += 1

    def before_model(self, ctx):
        self.counts["before_model"] += 1

    def after_model(self, ctx):
        self.counts["after_model"] += 1

    def after_agent(self, ctx):
        self.counts["after_agent"] += 1
        ctx.emit("probe_done", value=42)


def test_agent_fires_hooks_and_forwards_events_blocking(scripted_chat):
    scripted_chat([make_message("final")])
    probe = _Probe()
    seen: list[tuple[str, dict]] = []
    agent = LeadAgent(
        on_event=lambda k, p: seen.append((k, p)),
        middleware_factory=lambda: [probe],
    )
    out = agent.run("hi")

    assert out == "final"
    assert probe.counts["before_agent"] == 1
    assert probe.counts["after_agent"] == 1
    assert probe.counts["before_model"] == 1  # one model call → one final answer
    assert probe.counts["after_model"] == 1
    # The emitted middleware event reached on_event with type stripped to kind.
    assert ("probe_done", {"value": 42}) in seen


def test_agent_fires_hooks_and_forwards_events_streaming(scripted_stream):
    scripted_stream([[text_chunk("hel"), text_chunk("lo")]])
    probe = _Probe()
    agent = LeadAgent(middleware_factory=lambda: [probe])

    events = list(agent.run_stream("hi"))
    kinds = [e["type"] for e in events]

    # Standard events plus the forwarded middleware event.
    assert "final" in kinds
    assert any(e["type"] == "probe_done" and e["value"] == 42 for e in events)
    final = [e for e in events if e["type"] == "final"][0]
    assert final["content"] == "hello"
    assert probe.counts == {
        "before_agent": 1,
        "before_model": 1,
        "after_model": 1,
        "after_agent": 1,
    }


# --- TitleMiddleware --------------------------------------------------------
def test_title_middleware_sets_title_after_first_turn(scripted_chat, monkeypatch):
    scripted_chat([make_message("some answer")])
    monkeypatch.setattr(llm, "chat_completion", lambda *a, **k: "Nice Short Title")

    agent = LeadAgent(middleware_factory=lambda: [TitleMiddleware()])
    agent.run("please help me plan a trip")

    assert agent.title == "Nice Short Title"


def test_title_middleware_falls_back_on_llm_failure(scripted_chat, monkeypatch):
    scripted_chat([make_message("answer")])

    def boom(*a, **k):
        raise RuntimeError("no api")

    monkeypatch.setattr(llm, "chat_completion", boom)

    agent = LeadAgent(middleware_factory=lambda: [TitleMiddleware()])
    agent.run("teach me about black holes")

    # Fallback = trimmed first user message.
    assert agent.title == "teach me about black holes"


def test_title_middleware_noop_on_resumed_thread(scripted_chat, monkeypatch):
    scripted_chat([make_message("answer 2")])
    calls: list[int] = []
    monkeypatch.setattr(
        llm, "chat_completion", lambda *a, **k: calls.append(1) or "should not run"
    )

    history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "first answer"},
    ]
    agent = LeadAgent(middleware_factory=lambda: [TitleMiddleware()])
    agent.run("second question", history=history)

    # A prior assistant turn existed → titling is skipped entirely.
    assert agent.title is None
    assert calls == []


# --- TodoListMiddleware -----------------------------------------------------
def test_todo_middleware_injects_tool_and_prompt_note():
    mw = TodoListMiddleware()
    ctx = AgentContext(
        messages=[{"role": "system", "content": "base"}], tools=[]
    )
    mw.before_agent(ctx)

    assert any(getattr(t, "name", None) == "write_todos" for t in ctx.tools)
    sys_msg = ctx.messages[0]["content"]
    assert "write_todos" in sys_msg  # usage note appended


def test_todo_reminder_visible_to_model_but_not_persisted():
    mw = TodoListMiddleware()
    ctx = AgentContext(messages=[{"role": "system", "content": "base"}], tools=[])
    mw.before_agent(ctx)

    # Simulate the model calling write_todos.
    tool = next(t for t in ctx.tools if t.name == "write_todos")
    tool.run(
        '{"todos": [{"content": "step one", "status": "in_progress"},'
        ' {"content": "step two", "status": "pending"}]}'
    )

    # before_model injects an ephemeral [todo-list] reminder for the model.
    mw.before_model(ctx)
    reminders = [
        m for m in ctx.messages
        if m["role"] == "system" and m["content"].startswith("[todo-list]")
    ]
    assert len(reminders) == 1
    assert "[~] step one" in reminders[0]["content"]
    assert "[ ] step two" in reminders[0]["content"]

    # A second before_model must not stack reminders.
    mw.before_model(ctx)
    reminders = [
        m for m in ctx.messages
        if m["role"] == "system" and m["content"].startswith("[todo-list]")
    ]
    assert len(reminders) == 1

    # after_agent strips the reminder and publishes the plan to ctx.todos.
    mw.after_agent(ctx)
    assert not any(
        m["role"] == "system" and m["content"].startswith("[todo-list]")
        for m in ctx.messages
    )
    assert [t["content"] for t in ctx.todos] == ["step one", "step two"]


# --- SummarizationMiddleware ------------------------------------------------
def test_summarization_noop_under_budget(monkeypatch):
    called: list[int] = []
    monkeypatch.setattr(
        llm, "chat_completion", lambda *a, **k: called.append(1) or "summary"
    )
    mw = SummarizationMiddleware(max_tokens=10_000, keep_last=2)
    ctx = AgentContext(messages=[{"role": "user", "content": "short"}])
    before = list(ctx.messages)
    mw.before_model(ctx)
    assert ctx.messages == before  # untouched
    assert called == []  # never summarized


def test_summarization_compresses_and_preserves_head_and_tail(monkeypatch):
    monkeypatch.setattr(llm, "chat_completion", lambda *a, **k: "SUMMARY")

    big = "x" * 8000  # two of these (~4000 tokens) exceed the 3000-token budget
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": big},
        {"role": "assistant", "content": big},
        {"role": "user", "content": "recent-1"},
        {"role": "assistant", "content": "recent-2"},
        {"role": "user", "content": "recent-3"},
    ]
    mw = SummarizationMiddleware(max_tokens=3000, keep_last=3)
    ctx = AgentContext(messages=list(messages))
    mw.before_model(ctx)

    # Head system prompt kept, a summary system message injected, tail verbatim.
    assert ctx.messages[0] == {"role": "system", "content": "system prompt"}
    assert ctx.messages[1]["role"] == "system"
    assert ctx.messages[1]["content"].startswith("[conversation-summary]")
    assert "SUMMARY" in ctx.messages[1]["content"]
    assert ctx.messages[-3:] == messages[-3:]
    # An emitted context_compressed event carries the accounting.
    ev = [e for e in ctx.events if e["type"] == "context_compressed"]
    assert ev and ev[0]["kept"] == 3


def test_summarization_never_orphans_a_tool_result(monkeypatch):
    monkeypatch.setattr(llm, "chat_completion", lambda *a, **k: "SUMMARY")

    big = "y" * 16000
    # The naive keep_last=2 boundary would start the tail on a `tool` message;
    # the middleware must move the cut earlier so the tail begins on assistant.
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": big},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "bash", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "tool output"},
        {"role": "assistant", "content": "done"},
    ]
    mw = SummarizationMiddleware(max_tokens=3000, keep_last=2)
    ctx = AgentContext(messages=list(messages))
    mw.before_model(ctx)

    # The tail must not begin with a dangling tool message.
    tail_start = ctx.messages[2]  # [system prompt, summary, <tail...>]
    assert tail_start["role"] != "tool"


# --- default_middlewares stack ----------------------------------------------
def test_default_middlewares_order():
    stack = default_middlewares()
    assert [type(m).__name__ for m in stack] == [
        "TitleMiddleware",
        "SummarizationMiddleware",
        "TodoListMiddleware",
    ]
