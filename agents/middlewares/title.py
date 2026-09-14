"""TitleMiddleware — P4 automatic thread title (标题自动生成).

DeerFlow generates a short, human-friendly title for each thread from the first
user message so the sidebar reads nicely. This is the minimal counterpart:

- It runs in the ``after_agent`` hook, once, after the first assistant turn is
  produced — that's the earliest point we have both the question and enough
  signal to title it well.
- It asks the LLM for a <=8-word title in one cheap call; on any failure it
  falls back to a trimmed prefix of the first user message (so a title is
  *always* set, exactly like the store's ``_derive_title``).
- The result lands on ``ctx.title``; the agent copies it onto ``self.title`` and
  the web layer persists / returns it. It only fires once per thread: if the
  seeded history already produced a title in an earlier run, it no-ops.

Zero new dependencies: reuses ``llm.chat_completion`` and the store's titling
convention.
"""
from __future__ import annotations

from typing import Any

import llm
from agents.middlewares.base import AgentContext, Middleware

_TITLE_PROMPT = (
    "Generate a concise thread title (at most 8 words, no quotes, no trailing "
    "punctuation) that captures the user's request. Reply with the title only."
)


def _first_user_text(messages: list[dict[str, Any]]) -> str:
    for m in messages:
        if m.get("role") == "user":
            text = (m.get("content") or "").strip()
            if text:
                return text
    return ""


def _fallback_title(text: str, limit: int = 60) -> str:
    """Trimmed first-line prefix — mirrors store._derive_title's behaviour."""
    return text.strip().replace("\n", " ")[:limit]


class TitleMiddleware(Middleware):
    """Derive a thread title once, after the first assistant reply.

    Args:
        model:       Optional model override for the title call.
        only_first:  When True (default) skip if a title already exists in the
                     seeded history (an assistant turn preceding this run).
    """

    name = "title"

    def __init__(self, model: str | None = None, only_first: bool = True) -> None:
        self.model = model
        self.only_first = only_first

    def before_agent(self, ctx: AgentContext) -> None:
        # Detect whether this thread already had a completed assistant turn
        # before this run began (i.e. it was titled in a previous run).
        prior_assistant = any(
            m.get("role") == "assistant" and (m.get("content") or "").strip()
            for m in ctx.messages
        )
        ctx.scratch["title_already_done"] = prior_assistant

    def after_agent(self, ctx: AgentContext) -> None:
        if self.only_first and ctx.scratch.get("title_already_done"):
            return
        if ctx.title:  # another middleware / caller already set one
            return

        question = ctx.question or _first_user_text(ctx.messages)
        if not question:
            return

        ctx.title = self._make_title(question)
        ctx.emit("title", title=ctx.title)

    def _make_title(self, question: str) -> str:
        try:
            title = llm.chat_completion(
                [
                    {"role": "system", "content": _TITLE_PROMPT},
                    {"role": "user", "content": question},
                ],
                model=self.model,
                temperature=0.0,
            ).strip().strip('"').strip()
            # Guard against a model that ignores the length hint.
            if title and len(title) <= 80:
                return title
        except Exception:
            pass
        # Always return *some* title.
        return _fallback_title(question)
