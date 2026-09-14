"""SummarizationMiddleware — P4 context compression (超长上下文自动压缩).

DeerFlow's ``SummarizationMiddleware`` watches the running conversation and,
once it grows past a budget, replaces the oldest turns with a short LLM-written
summary so the context window never overflows. This is the minimal counterpart:

- It runs in the ``before_model`` hook, i.e. right before each model call, so
  the compression is always applied to what the model is about to see.
- "Length" is measured with a cheap, dependency-free token estimate
  (``~len(text)/4`` chars-per-token, summed over messages). No tokenizer.
- When the estimate exceeds ``max_tokens``, the middleware keeps the leading
  system prompt and the most recent ``keep_last`` messages verbatim, and folds
  everything in between into a single summary ``system`` message produced by a
  one-shot ``llm.chat_completion`` call.
- Tool-call integrity is preserved: we never split an assistant ``tool_calls``
  message from its matching ``tool`` results when choosing the cut point, so the
  provider never sees a dangling tool call.

Compression is idempotent-ish: the summary message is tagged in ``scratch`` so
repeated calls fold *new* history into a fresh summary rather than stacking
summaries of summaries indefinitely within one run.
"""
from __future__ import annotations

from typing import Any

import llm
from agents.middlewares.base import AgentContext, Middleware

# Marker so we can recognise (and replace) a summary we previously injected.
_SUMMARY_MARKER = "[conversation-summary]"

_SUMMARY_PROMPT = (
    "You are compressing a long assistant/tool conversation to fit a context "
    "window. Write a concise summary (a few sentences) that preserves the "
    "user's goals, key facts discovered, decisions made, and any pending "
    "action. Do not add commentary; output only the summary text."
)


def _estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Rough token count: ~4 chars/token over all textual content.

    Dependency-free and good enough to decide *when* to compress. Counts the
    ``content`` string plus any tool-call argument strings.
    """
    total = 0
    for m in messages:
        content = m.get("content") or ""
        if isinstance(content, str):
            total += len(content)
        for call in m.get("tool_calls") or []:
            fn = call.get("function", {})
            total += len(fn.get("name", "")) + len(fn.get("arguments", "") or "")
    return total // 4


def _safe_cut_index(messages: list[dict[str, Any]], keep_last: int) -> int:
    """Pick a cut so the tail (kept verbatim) never starts on a ``tool`` message.

    A ``tool`` message must stay paired with the preceding assistant
    ``tool_calls`` message. If the naive cut would orphan tool results, we move
    the cut earlier until the tail begins on a non-tool message.
    """
    cut = max(0, len(messages) - keep_last)
    while 0 < cut < len(messages) and messages[cut].get("role") == "tool":
        cut -= 1
    return cut


class SummarizationMiddleware(Middleware):
    """Compress over-long history into a summary before each model call.

    Args:
        max_tokens:  Estimated-token budget that triggers compression.
        keep_last:   How many most-recent messages to preserve verbatim.
        model:       Optional model override for the summary call.
    """

    name = "summarization"

    def __init__(
        self,
        max_tokens: int = 3000,
        keep_last: int = 6,
        model: str | None = None,
    ) -> None:
        self.max_tokens = max_tokens
        self.keep_last = keep_last
        self.model = model

    def before_model(self, ctx: AgentContext) -> None:
        messages = ctx.messages
        if _estimate_tokens(messages) <= self.max_tokens:
            return

        # Preserve a leading system prompt (if any) outside the summarised span.
        head: list[dict[str, Any]] = []
        body = messages
        if messages and messages[0].get("role") == "system":
            head = [messages[0]]
            body = messages[1:]

        cut = _safe_cut_index(body, self.keep_last)
        if cut <= 0:
            return  # nothing old enough to fold; leave as-is

        to_summarize = body[:cut]
        tail = body[cut:]

        summary_text = self._summarize(to_summarize)
        if not summary_text:
            return  # summary failed — better to keep full history than lose it

        summary_msg = {
            "role": "system",
            "content": f"{_SUMMARY_MARKER} {summary_text}",
        }
        # Rewrite the working conversation in place so the model call downstream
        # sees the compressed version.
        ctx.messages[:] = [*head, summary_msg, *tail]
        ctx.scratch["summarized_at_step"] = ctx.step
        ctx.emit(
            "context_compressed",
            dropped=len(to_summarize),
            kept=len(tail),
        )

    def _summarize(self, messages: list[dict[str, Any]]) -> str:
        """Produce a one-shot textual summary of ``messages`` via the LLM."""
        transcript = _render_transcript(messages)
        try:
            return llm.chat_completion(
                [
                    {"role": "system", "content": _SUMMARY_PROMPT},
                    {"role": "user", "content": transcript},
                ],
                model=self.model,
                temperature=0.0,
            ).strip()
        except Exception:
            return ""  # caller keeps full history on failure


def _render_transcript(messages: list[dict[str, Any]]) -> str:
    """Flatten messages into a plain-text transcript for the summariser."""
    lines: list[str] = []
    for m in messages:
        role = m.get("role", "?")
        content = m.get("content") or ""
        if m.get("tool_calls"):
            names = ", ".join(
                c.get("function", {}).get("name", "?") for c in m["tool_calls"]
            )
            content = f"{content} [called tools: {names}]".strip()
        lines.append(f"{role}: {content}")
    return "\n".join(lines)
