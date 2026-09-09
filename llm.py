"""LLM client wrapper over the Volcengine Ark SDK.

This is the single place that talks to the model provider. It refactors the two
reference scripts into reusable functions:

- `chat_completion(...)`  <- generalizes call_llm.py (returns just text)
- `chat(...)`             <- P1: full completion incl. tool_calls (for ReAct)
- `stream_chat(...)`      <- P2: yield streaming chunks (content + tool-call deltas)
- `embed(...)`            <- generalizes embedding_model.py

Swapping providers later (P6 "多模型工厂") only requires changing this file.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any, Iterator

from volcenginesdkarkruntime import Ark

import config


@lru_cache(maxsize=1)
def _client() -> Ark:
    """Lazily build one shared Ark client (reads key at first use)."""
    return Ark(api_key=config.require_api_key())


def chat(
    messages: list[dict[str, Any]],
    model: str | None = None,
    temperature: float = 0.7,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | None = None,
) -> Any:
    """Run one chat completion and return the raw assistant *message* object.

    P1 needs more than the reply text: when the model decides to call a tool it
    puts that decision in `message.tool_calls`. Returning the whole message lets
    the ReAct loop inspect `tool_calls` and append the message verbatim to the
    running conversation.

    `tools` is the OpenAI/Ark-style tools array (see Tool.to_openai_schema()).
    When omitted, this behaves like a plain completion.
    """
    kwargs: dict[str, Any] = {
        "model": model or config.CHAT_MODEL,
        "messages": messages,
        "temperature": temperature,
    }
    if tools:
        kwargs["tools"] = tools
        # Default to "auto": let the model decide whether to call a tool.
        kwargs["tool_choice"] = tool_choice or "auto"

    completion = _client().chat.completions.create(**kwargs)
    return completion.choices[0].message


def stream_chat(
    messages: list[dict[str, Any]],
    model: str | None = None,
    temperature: float = 0.7,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | None = None,
) -> Iterator[Any]:
    """Run one *streaming* chat completion, yielding raw SDK delta chunks.

    P2 (Web/SSE) needs the model's output as it is produced so the server can
    forward it token-by-token over Server-Sent Events. This mirrors `chat()`
    but sets `stream=True`; each yielded item is a completion *chunk* whose
    `choices[0].delta` carries incremental `content` and/or `tool_calls`.

    The caller (the agent's streaming ReAct loop) reassembles the deltas back
    into whole messages / tool calls. Keeping that reassembly out of this layer
    preserves the "single point that talks to the provider" contract: swapping
    providers in P6 only touches this file.
    """
    kwargs: dict[str, Any] = {
        "model": model or config.CHAT_MODEL,
        "messages": messages,
        "temperature": temperature,
        "stream": True,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = tool_choice or "auto"

    stream = _client().chat.completions.create(**kwargs)
    for chunk in stream:
        yield chunk


def chat_completion(
    messages: list[dict[str, Any]],
    model: str | None = None,
    temperature: float = 0.7,
) -> str:
    """Single-turn / multi-turn chat completion. Returns the reply text.

    `messages` follows the OpenAI-style schema, e.g.
        [{"role": "user", "content": "hello"}]
    Kept for callers (and earlier phases) that only want the text back.
    """
    message = chat(messages, model=model, temperature=temperature)
    return message.content or ""


def embed(
    inputs: list[dict[str, Any]],
    model: str | None = None,
    encoding_format: str = "float",
) -> Any:
    """Multimodal embedding. `inputs` is a list of text/image_url/video_url parts.

    Mirrors embedding_model.py; returns the raw SDK response so callers can read
    vectors / usage as needed. Used later by the RAG / memory phases.
    """
    return _client().multimodal_embeddings.create(
        model=model or config.EMBEDDING_MODEL,
        encoding_format=encoding_format,
        input=inputs,
    )
