"""Ark (Volcengine Doubao) chat + embedding adapter — P6.

Delegates to the ``volcenginesdkarkruntime`` client that the P0 wrapper already
knows how to build. Instead of duplicating the SDK bootstrap here we reuse the
lazily-cached client via a small factory hook — that way tests that patch
``llm._client`` keep working, and P0's env-driven ``ARK_API_KEY`` /
``CHAT_MODEL`` remains the source of truth when no ``models.yaml`` overrides it.

Ark speaks the OpenAI-style protocol so the adapter is essentially a signature
translation: the factory hands us ``**extra`` kwargs (``thinking`` / ``vision``
switches, custom ``top_p``, …) and we forward the ones the SDK understands.
"""
from __future__ import annotations

from typing import Any, Callable, Iterator

from models.base import BaseChatModel, BaseEmbeddingModel, ModelCapabilities

# The client-factory hook. Kept as a module attribute so tests can swap it in
# without monkeypatching a specific class instance. Default: reuse the P0
# lazily-cached Ark client via ``llm._client()`` so a single API key is loaded
# once per process and the offline test suite keeps patching one entry point.
_client_factory: Callable[[], Any] | None = None


def _default_client_factory() -> Any:
    """Lazy import: only reach into ``llm._client`` when actually called.

    Importing at module top level would create a hard cycle because ``llm.py``
    itself imports from :mod:`models`. Resolving inside the function keeps the
    import graph acyclic.
    """
    import llm

    return llm._client()


def get_client() -> Any:
    """Return the underlying Ark SDK client (honouring test overrides)."""
    factory = _client_factory or _default_client_factory
    return factory()


def set_client_factory(factory: Callable[[], Any] | None) -> None:
    """Install (or clear) a custom client factory. Used by tests."""
    global _client_factory
    _client_factory = factory


class ArkChatModel(BaseChatModel):
    """Chat completions over the Ark SDK.

    Args:
        model:        The Ark model id (e.g. ``doubao-seed-1-6-250615``).
        thinking:     Enable Ark's reasoning channel (forwarded to the SDK as
                      ``extra_body={"thinking": ...}``). Ignored by models that
                      do not support it, so leaving it on is safe.
        vision:       Advertise vision capability so upstream code can send
                      image parts in ``content``. No wire change — Ark accepts
                      the multimodal shape natively when the model supports it.
        name:         Optional friendly alias (defaults to the model id).
        default_temperature: Applied when the caller passes no ``temperature``.
    """

    def __init__(
        self,
        model: str,
        *,
        thinking: bool = False,
        vision: bool = False,
        name: str | None = None,
        default_temperature: float = 0.7,
    ) -> None:
        self.model = model
        self.thinking = thinking
        self.name = name or model
        self.default_temperature = default_temperature
        self.capabilities = ModelCapabilities(
            thinking=thinking, vision=vision, streaming=True
        )

    # -- internals ---------------------------------------------------------
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
        """Assemble the ``chat.completions.create`` kwargs.

        The blocking and streaming paths differ only in the ``stream`` flag, so
        the assembly lives here. ``extra`` is the free-form caller kwargs (like
        ``thinking``); we merge Ark-specific ones into ``extra_body`` and pass
        the rest through.
        """
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

        # Per-call thinking override; falls back to the constructor default.
        thinking = extra.pop("thinking", self.thinking)
        extra_body: dict[str, Any] = dict(extra.pop("extra_body", {}) or {})
        if thinking:
            # Ark's server accepts a small object; a boolean would also work but
            # the object form is what the SDK docs recommend and it round-trips
            # cleanly through OpenAI-compatible proxies.
            extra_body.setdefault("thinking", {"type": "enabled"})
        if extra_body:
            payload["extra_body"] = extra_body

        # Anything left in ``extra`` is a straight passthrough (top_p, seed, ...).
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
        completion = get_client().chat.completions.create(**payload)
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
        for chunk in get_client().chat.completions.create(**payload):
            yield chunk


class ArkEmbeddingModel(BaseEmbeddingModel):
    """Multimodal embeddings via Ark's ``multimodal_embeddings`` endpoint."""

    def __init__(self, model: str, *, name: str | None = None) -> None:
        self.model = model
        self.name = name or model

    def embed(
        self,
        inputs: list[dict[str, Any]],
        *,
        encoding_format: str = "float",
        **extra: Any,
    ) -> Any:
        return get_client().multimodal_embeddings.create(
            model=self.model,
            encoding_format=encoding_format,
            input=inputs,
            **extra,
        )
