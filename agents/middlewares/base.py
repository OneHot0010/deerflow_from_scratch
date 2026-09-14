"""Middleware base classes — P4 (中间件链).

Three pieces:

- ``AgentContext``   — the mutable state that flows through the chain for one
  ``run``/``run_stream`` call. It carries the working ``messages`` list (the
  same OpenAI-style dicts the agent already passes to ``llm``), the tool list,
  a free-form ``scratch`` dict for middleware-private state, and derived
  outputs (``title``, ``todos``) the web layer can surface.
- ``Middleware``     — an ABC exposing four **optional** lifecycle hooks. A
  concrete middleware overrides only the hooks it needs; the defaults are
  no-ops, so a middleware stays tiny and focused.
- ``MiddlewareChain`` — the registration + ordered-execution mechanism. It runs
  ``before_agent`` / ``before_model`` first→last and ``after_model`` /
  ``after_agent`` last→first, so the stack nests like context managers.

Hook contract (mirrors DeerFlow's LangGraph middleware, minus the graph):

    before_agent(ctx)   once, before the ReAct loop starts
      before_model(ctx) before every model call (may rewrite ctx.messages)
      after_model(ctx)  after every model reply (may inspect / annotate)
    after_agent(ctx)    once, after the loop produces a final answer

Hooks mutate ``ctx`` in place and return ``None``. Any exception a hook raises
is swallowed by the chain (observability/enrichment must never break the core
loop) — the offending middleware simply has no effect for that call.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentContext:
    """Mutable state threaded through the middleware chain for one run.

    Attributes:
        messages:  The live conversation (OpenAI-style dicts). ``before_model``
                   hooks may rewrite this in place (e.g. summarization).
        tools:     The ``Tool`` objects available this run. Middlewares may
                   append tools (e.g. TodoList adds ``write_todos``) *before*
                   the agent snapshots schemas.
        question:  The user question that started this run.
        step:      0-based index of the current model call (bumped by the agent
                   before each ``before_model`` fire).
        scratch:   Free-form per-run storage for middleware-private state.
        title:     Derived thread title (set by TitleMiddleware), or None.
        todos:     Current task list (managed by TodoListMiddleware).
        events:    Optional sink; middlewares may append structured events the
                   agent forwards to the SSE stream. Each is a ``dict`` with a
                   ``type`` key, matching the agent's event protocol.
    """

    messages: list[dict[str, Any]]
    tools: list[Any] = field(default_factory=list)
    question: str = ""
    step: int = 0
    scratch: dict[str, Any] = field(default_factory=dict)
    title: str | None = None
    todos: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)

    def emit(self, event_type: str, **payload: Any) -> None:
        """Queue a structured event for the agent to forward (e.g. over SSE)."""
        self.events.append({"type": event_type, **payload})


class Middleware:
    """Base class for a lifecycle middleware. Override only the hooks you need.

    ``name`` defaults to the class name and is used for logging/ordering aids.
    All four hooks are no-ops by default so subclasses stay minimal.
    """

    #: Human-readable identifier; override on subclasses if desired.
    name: str = "middleware"

    def before_agent(self, ctx: AgentContext) -> None:
        """Run once before the ReAct loop starts (setup, tool injection)."""

    def before_model(self, ctx: AgentContext) -> None:
        """Run before each model call (may rewrite ``ctx.messages``)."""

    def after_model(self, ctx: AgentContext) -> None:
        """Run after each model reply (inspect / annotate state)."""

    def after_agent(self, ctx: AgentContext) -> None:
        """Run once after the loop yields a final answer (derive outputs)."""


class MiddlewareChain:
    """Ordered collection of middlewares + the mechanism that drives them.

    Registration is explicit and ordered; the chain runs ``before_*`` hooks in
    registration order and ``after_*`` hooks in reverse, so behaviour nests like
    a stack of context managers (the first-registered middleware is the
    outermost wrapper). A hook that raises is isolated: the error is recorded on
    ``ctx.scratch['_errors']`` and the chain continues, because enrichment must
    never break the agent's core loop.
    """

    def __init__(self, middlewares: list[Middleware] | None = None) -> None:
        self._middlewares: list[Middleware] = []
        for mw in middlewares or []:
            self.register(mw)

    # -- registration --------------------------------------------------------
    def register(self, middleware: Middleware) -> "MiddlewareChain":
        """Append a middleware to the chain (returns self for chaining)."""
        if not isinstance(middleware, Middleware):
            raise TypeError(
                f"expected a Middleware instance, got {type(middleware).__name__}"
            )
        self._middlewares.append(middleware)
        return self

    def __len__(self) -> int:
        return len(self._middlewares)

    @property
    def middlewares(self) -> list[Middleware]:
        """A copy of the registered middlewares, in execution order."""
        return list(self._middlewares)

    # -- execution -----------------------------------------------------------
    def before_agent(self, ctx: AgentContext) -> None:
        for mw in self._middlewares:
            self._safe(mw, "before_agent", ctx)

    def before_model(self, ctx: AgentContext) -> None:
        for mw in self._middlewares:
            self._safe(mw, "before_model", ctx)

    def after_model(self, ctx: AgentContext) -> None:
        for mw in reversed(self._middlewares):
            self._safe(mw, "after_model", ctx)

    def after_agent(self, ctx: AgentContext) -> None:
        for mw in reversed(self._middlewares):
            self._safe(mw, "after_agent", ctx)

    # -- internals -----------------------------------------------------------
    @staticmethod
    def _safe(mw: Middleware, hook: str, ctx: AgentContext) -> None:
        """Invoke one hook, isolating any error so the chain keeps running."""
        try:
            getattr(mw, hook)(ctx)
        except Exception as e:  # enrichment must never break the core loop
            ctx.scratch.setdefault("_errors", []).append(
                {"middleware": getattr(mw, "name", type(mw).__name__), "hook": hook,
                 "error": f"{type(e).__name__}: {e}"}
            )
