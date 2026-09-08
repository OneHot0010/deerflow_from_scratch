"""LLM client wrapper over the Volcengine Ark SDK.

This is the single place that talks to the model provider. It refactors the two
reference scripts into reusable functions:

- `chat_completion(...)`  <- generalizes call_llm.py
- `embed(...)`            <- generalizes embedding_model.py

Swapping providers later (P6 "多模型工厂") only requires changing this file.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

from volcenginesdkarkruntime import Ark

import config


@lru_cache(maxsize=1)
def _client() -> Ark:
    """Lazily build one shared Ark client (reads key at first use)."""
    return Ark(api_key=config.require_api_key())


def chat_completion(
    messages: list[dict[str, Any]],
    model: str | None = None,
    temperature: float = 0.7,
) -> str:
    """Single-turn / multi-turn chat completion. Returns the reply text.

    `messages` follows the OpenAI-style schema, e.g.
        [{"role": "user", "content": "hello"}]
    Mirrors call_llm.py but returns just the text so callers stay simple.
    """
    completion = _client().chat.completions.create(
        model=model or config.CHAT_MODEL,
        messages=messages,
        temperature=temperature,
    )
    return completion.choices[0].message.content or ""


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
