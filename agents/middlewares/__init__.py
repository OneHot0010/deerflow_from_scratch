"""Middleware chain for mini-deerflow — P4 (中间件链).

DeerFlow wraps its agent in a stack of 40+ *middlewares* (see
``deerflow/agents/middlewares/``): small, composable units that hook into the
agent's lifecycle to add cross-cutting behaviour — automatic thread titles,
context compression, task tracking, safety rails, and so on. Each middleware
implements a subset of lifecycle hooks (``before_model`` / ``after_model`` /
``before_agent`` / ``after_agent`` in DeerFlow's LangGraph flavour) and the
framework runs them as an ordered chain around every model call.

This package is the minimal, zero-new-dependency counterpart, in the spirit of
the earlier phases:

- ``base.py``   — the ``Middleware`` ABC (lifecycle hooks) + the shared
  ``AgentContext`` that flows through them, and ``MiddlewareChain`` (the
  registration + ordered-execution mechanism).
- ``summarization.py`` — compress an over-long conversation before the model
  call (roadmap acceptance: "超长上下文自动压缩").
- ``title.py``  — derive a human-friendly thread title after the first
  assistant turn (roadmap acceptance: "标题自动生成").
- ``todo.py``   — a task-tracking middleware that exposes a ``write_todos`` tool
  and keeps the current plan visible to the model (milestone: "任务跟踪").

The lead agent owns one ``MiddlewareChain`` and calls its hooks at fixed points
in the ReAct loop; middlewares never talk to the model provider directly (that
stays behind ``llm.py``), preserving the single-point-of-contact contract.

``default_middlewares()`` returns the built-in stack in a sensible order. The
agent defaults to an *empty* chain so earlier phases' behaviour (and their
offline tests) are untouched; the web layer opts in to the full stack.
"""
from __future__ import annotations

from agents.middlewares.base import AgentContext, Middleware, MiddlewareChain
from agents.middlewares.summarization import SummarizationMiddleware
from agents.middlewares.title import TitleMiddleware
from agents.middlewares.todo import TodoListMiddleware


def default_middlewares() -> list[Middleware]:
    """Return the built-in middleware stack in execution order.

    Order matters for ``before_model`` (they run first→last around each model
    call) and is reversed for the ``after_*`` hooks so the chain nests like a
    stack of context managers:

    1. ``TitleMiddleware``        — cheap, sets a title once after turn 1.
    2. ``SummarizationMiddleware`` — compresses history right before the model.
    3. ``TodoListMiddleware``     — injects the live plan closest to the model.
    """
    return [
        TitleMiddleware(),
        SummarizationMiddleware(),
        TodoListMiddleware(),
    ]


__all__ = [
    "AgentContext",
    "Middleware",
    "MiddlewareChain",
    "SummarizationMiddleware",
    "TitleMiddleware",
    "TodoListMiddleware",
    "default_middlewares",
]
