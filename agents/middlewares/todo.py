"""TodoListMiddleware — P4 task tracking (任务跟踪).

DeerFlow's TodoList/TodoWrite middleware lets the agent maintain a live task
list: it exposes a ``write_todos`` tool the model calls to (re)write its plan,
and keeps that plan in front of the model on every turn so a long task stays on
track. This is the minimal counterpart.

Two lifecycle touch-points:

- ``before_agent``: inject a ``write_todos`` tool into ``ctx.tools`` (before the
  agent snapshots tool schemas) and extend the system prompt with a short usage
  note. The tool's implementation closes over this middleware instance, so a
  call mutates ``self._todos`` — the single source of truth for the run.
- ``before_model``: if there are todos, append an ephemeral ``system`` reminder
  listing the current plan with ``[x]``/``[ ]`` markers, so the model always
  sees its outstanding work. The reminder is stripped and re-added each turn so
  it never accumulates.

``after_model`` / ``after_agent`` copy the current plan onto ``ctx.todos`` so
the web layer can surface it. No new dependency — the tool reuses the existing
``Tool`` abstraction.
"""
from __future__ import annotations

import json
from typing import Any

from agents.middlewares.base import AgentContext, Middleware
from tools import Tool

_REMINDER_MARKER = "[todo-list]"

_TOOL_DESCRIPTION = (
    "Record or update your task plan for a multi-step job. Call this with the "
    "full list of todos whenever the plan changes. Each todo is an object with "
    "`content` (str) and `status` (one of: pending, in_progress, completed). "
    "Keep exactly one task in_progress at a time. Re-send the whole list each "
    "call; it replaces the previous plan."
)

_TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "todos": {
            "type": "array",
            "description": "The full, updated task list.",
            "items": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "The task."},
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "completed"],
                    },
                },
                "required": ["content", "status"],
            },
        }
    },
    "required": ["todos"],
}

_VALID_STATUS = {"pending", "in_progress", "completed"}
_MARKS = {"completed": "[x]", "in_progress": "[~]", "pending": "[ ]"}

_PROMPT_NOTE = (
    "\n\nFor multi-step tasks, use the `write_todos` tool to plan and track your "
    "progress: write the full task list up front, then update statuses as you "
    "go (exactly one task in_progress at a time)."
)


class TodoListMiddleware(Middleware):
    """Give the agent a live, model-writable task list.

    The plan lives on the middleware instance for the duration of a run; the
    caller should use a fresh middleware (or fresh chain) per run to avoid state
    bleeding across conversations. ``default_middlewares()`` builds fresh
    instances, and the agent builds its chain per run when given a factory.
    """

    name = "todo"

    def __init__(self) -> None:
        self._todos: list[dict[str, Any]] = []

    # -- setup ---------------------------------------------------------------
    def before_agent(self, ctx: AgentContext) -> None:
        self._todos = []  # reset per run
        # Inject the write_todos tool if not already present.
        if not any(getattr(t, "name", None) == "write_todos" for t in ctx.tools):
            ctx.tools.append(self._build_tool())
        # Extend the leading system prompt with a short usage note.
        for m in ctx.messages:
            if m.get("role") == "system":
                if _PROMPT_NOTE.strip() not in (m.get("content") or ""):
                    m["content"] = (m.get("content") or "") + _PROMPT_NOTE
                break

    # -- keep the plan in front of the model --------------------------------
    def before_model(self, ctx: AgentContext) -> None:
        # Drop any stale reminder we injected last turn.
        ctx.messages[:] = [
            m
            for m in ctx.messages
            if not (
                m.get("role") == "system"
                and isinstance(m.get("content"), str)
                and m["content"].startswith(_REMINDER_MARKER)
            )
        ]
        if not self._todos:
            return
        ctx.messages.append(
            {"role": "system", "content": self._render_reminder()}
        )

    def after_model(self, ctx: AgentContext) -> None:
        ctx.todos = list(self._todos)

    def after_agent(self, ctx: AgentContext) -> None:
        ctx.todos = list(self._todos)
        # Leave no ephemeral reminder in the persisted conversation.
        ctx.messages[:] = [
            m
            for m in ctx.messages
            if not (
                m.get("role") == "system"
                and isinstance(m.get("content"), str)
                and m["content"].startswith(_REMINDER_MARKER)
            )
        ]

    # -- the tool ------------------------------------------------------------
    def _build_tool(self) -> Tool:
        def write_todos(todos: list[dict[str, Any]]) -> str:
            cleaned = self._normalize(todos)
            self._todos = cleaned
            return "Updated task list:\n" + self._render_plain(cleaned)

        return Tool(
            name="write_todos",
            description=_TOOL_DESCRIPTION,
            parameters=_TOOL_PARAMETERS,
            func=write_todos,
        )

    @staticmethod
    def _normalize(todos: Any) -> list[dict[str, Any]]:
        """Coerce model input into a clean list of {content, status} dicts."""
        if isinstance(todos, str):
            try:
                todos = json.loads(todos)
            except (json.JSONDecodeError, TypeError):
                todos = []
        out: list[dict[str, Any]] = []
        for item in todos or []:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content", "")).strip()
            if not content:
                continue
            status = item.get("status", "pending")
            if status not in _VALID_STATUS:
                status = "pending"
            out.append({"content": content, "status": status})
        return out

    def _render_reminder(self) -> str:
        return f"{_REMINDER_MARKER} Current plan:\n" + self._render_plain(self._todos)

    @staticmethod
    def _render_plain(todos: list[dict[str, Any]]) -> str:
        return "\n".join(
            f"{_MARKS.get(t['status'], '[ ]')} {t['content']}" for t in todos
        )
