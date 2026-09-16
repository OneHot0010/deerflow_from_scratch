"""OpenAI-compatible chat + embedding adapter — P6.

Fronts any endpoint that speaks the OpenAI Chat Completions protocol:

- OpenAI itself (``base_url=None`` or ``https://api.openai.com/v1``)
- DeepSeek (``base_url=https://api.deepseek.com/v1``)
- Anthropic via a compatibility proxy, Together, Groq, and so on.

The SDK dependency is loaded lazily so a user who only wants Ark does not need
``openai`` installed — the import lives inside :func:`_build_client` and the
error is a clear pip-install hint instead of an ``ImportError`` at import time.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any, Iterator

from models.base import BaseChatModel, BaseEmbeddingModel, ModelCapabilities


class _OpenAIClientMissingError(RuntimeError):
    """Raised when the ``openai`` package is not installed."""


@lru_cache(maxsize=32)
def _build_client(base_url: str | None, api_key_env: str | None, api_key: str | None) -> Any:
    """Build (or reuse) a shared OpenAI SDK client keyed by connection args.

    The LRU cache keeps one client per unique ``(base_url, key source)`` tuple
    so many model aliases pointing at the same endpoint share a single
    connection pool. Kept out of __init__ so tests can swap ``base_url`` /
    ``api_key`` freely without stale client leaks.
    """
    try:
        from openai import OpenAI  # type: ignore
    except ImportError as e:  # pragma: no cover - exercised only when the extra is missing
        raise _OpenAIClientMissingError(
            "The 'openai' package is required for OpenAI-compatible providers. "
            "Install with `pip install openai`."
        ) from e

    import os

    resolved_key = api_key
    if not resolved_key and api_key_env:
        resolved_key = os.environ.get(api_key_env)
    if not resolved_key:
        raise RuntimeError(
            "OpenAI-compatible provider: missing API key. Set the env var named "
            f"by `api_key_env` (currently {api_key_env!r}) or pass `api_key` in config."
        )

    kwargs: dict[str, Any] = {"api_key": resolved_key}
    if base_url:
        kwargs["base_url"] = base_url
    return OpenAI(**kwargs)


class OpenAIChatModel(BaseChatModel):
    """Chat completions over an OpenAI-compatible endpoint.

    Args:
        model:        Provider model id (e.g. ``gpt-4o-mini``, ``deepseek-chat``).
        base_url:     Optional endpoint override; ``None`` = OpenAI default.
        api_key:      Explicit key (avoid: prefer env). Overrides ``api_key_env``.
        api_key_env:  Env var to read the key from. Defaults to ``OPENAI_API_KEY``.
        thinking:     Whether to send ``extra_body={"reasoning": ...}`` for models
                      that expose a reasoning channel (DeepSeek-R1, o-series).
        vision:       Advertise multimodal capability (no wire change).
        name:         Optional alias (defaults to the model id).
        default_temperature: Applied when the caller passes no ``temperature``.
    """

    def __init__(
        self,
        model: str,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        api_key_env: str | None = "OPENAI_API_KEY",
        thinking: bool = False,
        vision: bool = False,
        name: str | None = None,
        default_temperature: float = 0.7,
    ) -> None:
        self.model = model
        self.base_url = base_url
        self._api_key = api_key
        self._api_key_env = api_key_env
        self.thinking = thinking
        self.name = name or model
        self.default_temperature = default_temperature
        self.capabilities = ModelCapabilities(
            thinking=thinking, vision=vision, streaming=True
        )

    # -- internals ---------------------------------------------------------
    def _client(self) -> Any:
        return _build_client(self.base_url, self._api_key_env, self._api_key)

    def _build_kwargs(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None,
        tools: list[dict[str, Any]] | None,
        tool_choice: str | None,
        stream: bool,
        extra: dict[str, Any],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.default_temperature if temperature is None else temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"
        if stream:
            payload["stream"] = True

        thinking = extra.pop("thinking", self.thinking)
        extra_body: dict[str, Any] = dict(extra.pop("extra_body", {}) or {})
        if thinking:
            # DeepSeek / OpenAI o-series both accept a "reasoning" hint via
            # extra_body; the exact key varies by provider so users can override
            # with an explicit ``extra_body`` if theirs differs.
            extra_body.setdefault("reasoning", {"effort": "medium"})
        if extra_body:
            payload["extra_body"] = extra_body
        payload.update(extra)
        return payload

    # -- BaseChatModel API -------------------------------------------------
    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        **extra: Any,
    ) -> Any:
        payload = self._build_kwargs(
            messages,
            temperature=temperature,
            tools=tools,
            tool_choice=tool_choice,
            stream=False,
            extra=extra,
        )
        completion = self._client().chat.completions.create(**payload)
        return completion.choices[0].message

    def stream_chat(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        **extra: Any,
    ) -> Iterator[Any]:
        payload = self._build_kwargs(
            messages,
            temperature=temperature,
            tools=tools,
            tool_choice=tool_choice,
            stream=True,
            extra=extra,
        )
        for chunk in self._client().chat.completions.create(**payload):
            yield chunk


class OpenAIEmbeddingModel(BaseEmbeddingModel):
    """Text embeddings via the ``/embeddings`` endpoint (OpenAI-compatible)."""

    def __init__(
        self,
        model: str,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        api_key_env: str | None = "OPENAI_API_KEY",
        name: str | None = None,
    ) -> None:
        self.model = model
        self.base_url = base_url
        self._api_key = api_key
        self._api_key_env = api_key_env
        self.name = name or model

    def embed(
        self,
        inputs: list[dict[str, Any]],
        *,
        encoding_format: str = "float",
        **extra: Any,
    ) -> Any:
        # OpenAI's ``embeddings`` endpoint takes a flat string (or list of
        # strings) rather than the multimodal parts array Ark uses. We flatten
        # ``[{"type": "text", "text": "..."}]`` into strings for the caller.
        flat = _flatten_embedding_inputs(inputs)
        return self._client().embeddings.create(
            model=self.model,
            input=flat,
            encoding_format=encoding_format,
            **extra,
        )

    def _client(self) -> Any:
        return _build_client(self.base_url, self._api_key_env, self._api_key)


def _flatten_embedding_inputs(inputs: list[dict[str, Any]]) -> list[str]:
    """Reduce Ark-style multimodal parts to plain text for OpenAI embeddings.

    OpenAI's embedding API is text-only; images / video would need a different
    provider. We keep the same input shape callers already use with
    ``llm.embed(...)`` and pull out the text parts, dropping non-text entries
    with an informative empty-string sentinel so caller indexing stays stable.
    """
    out: list[str] = []
    for item in inputs:
        if not isinstance(item, dict):
            out.append(str(item))
            continue
        kind = item.get("type", "text")
        if kind == "text":
            out.append(item.get("text", ""))
        else:
            out.append("")  # placeholder: OpenAI text-embeddings cannot embed images.
    return out
