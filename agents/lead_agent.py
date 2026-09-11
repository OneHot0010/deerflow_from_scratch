"""Lead agent — tool-calling ReAct (P1) + streaming (P2) + thread history (P3).

Grows DeerFlow's `agents/lead_agent` from the P0 single-turn responder into a
multi-turn ReAct loop:

    Reason -> (optional) Act via a tool -> Observe result -> Reason -> ... -> Answer

Public entry points:
- `run(question, history=None)`        -> P1: run the loop, return final text.
- `run_stream(question, history=None)` -> P2: run the loop as a generator of
                                          structured events for Server-Sent Events.

P3 (会话持久化) adds an optional `history` argument to both: prior messages from
a persisted thread seed the conversation instead of always starting fresh from
[system, user]. After either method finishes, the full working conversation is
available on `self.messages`, so the web layer can save it back to the thread
store. The SSE event protocol is unchanged.

Flow each iteration:
1. Send the running conversation + tool schemas to the LLM.
2. If the model returns `tool_calls`, execute each locally and append the
   results back as `tool` messages, then loop.
3. If the model returns plain content (no tool calls), that is the final answer.

Still no LangGraph — we drive the loop ourselves over the Ark SDK's OpenAI-style
function-calling protocol. Tools arrive from `tools.get_available_tools()`.
"""
from __future__ import annotations

from typing import Any, Callable, Iterator

import llm
from tools import Tool, get_available_tools, tools_by_name

SYSTEM_PROMPT = (
    "You are a capable assistant that can use tools to accomplish tasks. "
    "You have access to a local machine via the `bash`, `read_file`, and "
    "`write_file` tools. Think step by step: when a task needs real actions "
    "(inspecting the filesystem, running commands, reading or writing files), "
    "call the appropriate tool instead of guessing. When you have enough "
    "information, reply to the user directly with a clear final answer."
)

# Safety rail: cap how many reason->act cycles we run before giving up, so a
# confused model can never loop forever.
DEFAULT_MAX_STEPS = 10


class LeadAgent:
    """Multi-turn, tool-using agent (P1 blocking + P2 streaming + P3 history).

    Kept as a class so later phases can attach memory / sub-agents without
    changing the call site. `run(question)` returns the final answer text;
    `run_stream(question)` yields events for the SSE server. Both accept an
    optional `history` of prior messages (P3) and leave the full updated
    conversation on `self.messages` for the caller to persist.
    """

    def __init__(
        self,
        system_prompt: str = SYSTEM_PROMPT,
        tools: list[Tool] | None = None,
        max_steps: int = DEFAULT_MAX_STEPS,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.system_prompt = system_prompt
        self.tools = get_available_tools() if tools is None else tools
        self._tool_index = tools_by_name(self.tools)
        self._tool_schemas = [t.to_openai_schema() for t in self.tools]
        self.max_steps = max_steps
        # Optional observability hook: on_event(kind, payload). Used by the CLI
        # to show tool activity. Never affects control flow.
        self.on_event = on_event
        # The working conversation of the most recent run/run_stream call. After
        # a run finishes this holds [system, ...history..., user, ...loop...] so
        # the web layer (P3) can persist it back to the thread store.
        self.messages: list[dict[str, Any]] = []

    # -- conversation seeding (P3) ------------------------------------------
    def _seed_messages(
        self, question: str, history: list[dict[str, Any]] | None
    ) -> list[dict[str, Any]]:
        """Build the starting conversation for a run.

        With no `history`, this is the classic [system, user] pair. With a
        persisted `history` (P3), we reuse it verbatim — ensuring exactly one
        leading system prompt — and append the new user turn. This lets a thread
        resume where it left off, tool_calls and all.
        """
        if not history:
            return [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": question},
            ]

        seeded = list(history)
        if not seeded or seeded[0].get("role") != "system":
            seeded.insert(0, {"role": "system", "content": self.system_prompt})
        seeded.append({"role": "user", "content": question})
        return seeded

    # -- public API: blocking (P1, + P3 history) ----------------------------
    def run(self, question: str, history: list[dict[str, Any]] | None = None) -> str:
        """Take one user question, drive the ReAct loop, return the answer.

        `history` (P3) optionally seeds the conversation with a thread's prior
        messages. The full updated conversation is left on `self.messages`.
        """
        if not question or not question.strip():
            raise ValueError("question must be a non-empty string")

        messages = self._seed_messages(question, history)
        self.messages = messages

        for _ in range(self.max_steps):
            message = llm.chat(messages, tools=self._tool_schemas or None)
            tool_calls = getattr(message, "tool_calls", None)

            # No tool calls -> this is the final answer.
            if not tool_calls:
                messages.append(
                    {"role": "assistant", "content": message.content or ""}
                )
                return message.content or ""

            # Record the assistant's tool-call turn verbatim, then execute.
            messages.append(_assistant_message_to_dict(message))
            for call in tool_calls:
                messages.append(self._execute_tool_call(call))

        # Exhausted the step budget: make one last plain call for a wrap-up.
        self._emit("max_steps_reached", {"max_steps": self.max_steps})
        final = llm.chat(messages)  # no tools -> force a textual answer
        content = final.content or "[agent] stopped: reached max tool-call steps."
        messages.append({"role": "assistant", "content": content})
        return content

    # -- public API: streaming (P2, + P3 history) ---------------------------
    def run_stream(
        self, question: str, history: list[dict[str, Any]] | None = None
    ) -> Iterator[dict[str, Any]]:
        """Drive the ReAct loop as a generator of structured events.

        Yields dicts with a `type` field the web layer maps 1:1 onto SSE
        `event:` names:

        - {"type": "message_chunk", "delta": str}   incremental assistant text
        - {"type": "tool_start", "name": str, "arguments": str}
        - {"type": "tool_end",   "name": str, "result": str}
        - {"type": "max_steps",  "max_steps": int}
        - {"type": "final",      "content": str}   terminal answer text
        - {"type": "error",      "message": str}   terminal error

        `history` (P3) optionally seeds the conversation with a thread's prior
        messages; after the stream is drained the full updated conversation is
        available on `self.messages`. The same `on_event` observability hook
        fires for tool activity, so the CLI tracer keeps working unchanged.
        """
        if not question or not question.strip():
            yield {"type": "error", "message": "question must be a non-empty string"}
            return

        messages = self._seed_messages(question, history)
        self.messages = messages

        try:
            for _ in range(self.max_steps):
                # Stream one model turn, forwarding text deltas as they arrive
                # while reassembling the full message (content + tool_calls).
                content_parts: list[str] = []
                tool_calls_acc: dict[int, dict[str, Any]] = {}

                for chunk in llm.stream_chat(messages, tools=self._tool_schemas or None):
                    choices = getattr(chunk, "choices", None)
                    if not choices:
                        continue
                    delta = choices[0].delta

                    text = getattr(delta, "content", None)
                    if text:
                        content_parts.append(text)
                        yield {"type": "message_chunk", "delta": text}

                    for tc in getattr(delta, "tool_calls", None) or []:
                        _merge_tool_call_delta(tool_calls_acc, tc)

                # No tool calls this turn -> the streamed text was the answer.
                if not tool_calls_acc:
                    answer = "".join(content_parts)
                    messages.append({"role": "assistant", "content": answer})
                    yield {"type": "final", "content": answer}
                    return

                # Otherwise: record the assistant turn, run each tool, loop.
                ordered = [tool_calls_acc[i] for i in sorted(tool_calls_acc)]
                messages.append(
                    {
                        "role": "assistant",
                        "content": "".join(content_parts),
                        "tool_calls": ordered,
                    }
                )
                for call in ordered:
                    name = call["function"]["name"]
                    raw_args = call["function"]["arguments"]
                    self._emit("tool_start", {"name": name, "arguments": raw_args})
                    yield {"type": "tool_start", "name": name, "arguments": raw_args}

                    tool = self._tool_index.get(name)
                    result = (
                        f"[tool-error] unknown tool: {name}"
                        if tool is None
                        else tool.run(raw_args)
                    )

                    self._emit("tool_end", {"name": name, "result": result})
                    yield {"type": "tool_end", "name": name, "result": result}
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": result,
                        }
                    )

            # Step budget exhausted: one last plain (still streamed) wrap-up.
            self._emit("max_steps_reached", {"max_steps": self.max_steps})
            yield {"type": "max_steps", "max_steps": self.max_steps}
            final_parts: list[str] = []
            for chunk in llm.stream_chat(messages):  # no tools -> textual answer
                choices = getattr(chunk, "choices", None)
                if not choices:
                    continue
                text = getattr(choices[0].delta, "content", None)
                if text:
                    final_parts.append(text)
                    yield {"type": "message_chunk", "delta": text}
            content = (
                "".join(final_parts)
                or "[agent] stopped: reached max tool-call steps."
            )
            messages.append({"role": "assistant", "content": content})
            yield {"type": "final", "content": content}
        except Exception as e:  # never leak a raw traceback to the SSE client
            yield {"type": "error", "message": f"{type(e).__name__}: {e}"}

    # -- internals -----------------------------------------------------------
    def _execute_tool_call(self, call: Any) -> dict[str, Any]:
        """Run a single tool call and return its `tool` result message."""
        name = call.function.name
        raw_args = call.function.arguments
        self._emit("tool_start", {"name": name, "arguments": raw_args})

        tool = self._tool_index.get(name)
        if tool is None:
            result = f"[tool-error] unknown tool: {name}"
        else:
            result = tool.run(raw_args)

        self._emit("tool_end", {"name": name, "result": result})
        return {
            "role": "tool",
            "tool_call_id": call.id,
            "content": result,
        }

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        if self.on_event is not None:
            try:
                self.on_event(kind, payload)
            except Exception:
                pass  # observability must never break the loop


def _merge_tool_call_delta(acc: dict[int, dict[str, Any]], tc: Any) -> None:
    """Fold one streamed tool_call delta into the accumulator, keyed by index.

    Streaming splits a single tool call across many chunks: the first carries
    the id + function name, later ones append fragments of the JSON arguments
    string. We stitch them back together by their stable `index`.
    """
    idx = getattr(tc, "index", 0) or 0
    slot = acc.setdefault(
        idx,
        {"id": None, "type": "function", "function": {"name": "", "arguments": ""}},
    )
    if getattr(tc, "id", None):
        slot["id"] = tc.id
    fn = getattr(tc, "function", None)
    if fn is not None:
        if getattr(fn, "name", None):
            slot["function"]["name"] = fn.name
        if getattr(fn, "arguments", None):
            slot["function"]["arguments"] += fn.arguments


def _assistant_message_to_dict(message: Any) -> dict[str, Any]:
    """Convert an Ark assistant message (with tool_calls) into a plain dict.

    The SDK returns a rich object; when we append it to the running
    conversation for the next request it must be JSON-serializable and carry the
    tool_calls so the provider can match them to our `tool` results.
    """
    tool_calls = []
    for call in getattr(message, "tool_calls", None) or []:
        tool_calls.append(
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.function.name,
                    "arguments": call.function.arguments,
                },
            }
        )
    return {
        "role": "assistant",
        "content": message.content or "",
        "tool_calls": tool_calls,
    }
