"""LLM client wrapper — now backed by the P6 multi-model factory.

The public surface (``chat`` / ``chat_completion`` / ``stream_chat`` / ``embed``)
is unchanged, so the ReAct loop, SSE server, middlewares and every existing
test keeps working byte-for-byte. Under the hood the calls are delegated to
whichever :class:`~models.base.BaseChatModel` / :class:`BaseEmbeddingModel` the
factory resolves for the requested alias:

- When no ``models.yaml`` is present (the P0-P5 setup) the factory hands back a
  built-in Ark adapter that talks to :func:`_client` — the same lazily-cached
  Volcengine Ark client the earlier phases used. Existing tests that patch
  ``llm._client`` still work: the Ark adapter's ``get_client`` hook calls
  ``llm._client()`` internally.
- With a ``models.yaml`` (or ``MODELS_CONFIG`` env var), users can flip
  ``model="deepseek-chat"`` / ``model="gpt-4o-mini"`` / ... on any call site
  without touching this file. Thinking / vision toggles ride along via
  ``**extra`` kwargs the adapters cherry-pick.

The single-point-of-contact contract from the earlier phases stays intact —
this module is still the only place upstream code talks to a model provider;
we just no longer hard-code *which* provider.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any, Iterator

from volcenginesdkarkruntime import Ark

import config


@lru_cache(maxsize=1)
def _client() -> Ark:
    """Lazily build one shared Ark client (reads key at first use).

    Kept as a module-level lru_cached function for two reasons: (1) the offline
    test suite patches ``llm._client`` to inject a fake without going through
    the Ark constructor, and (2) the built-in Ark adapter (used as the P0
    fallback when no ``models.yaml`` is loaded) reaches into this function via
    ``models.providers.ark.get_client`` — so everything shares one client.
    """
    return Ark(api_key=config.require_api_key())


def _chat_model(alias: str | None):
    """Resolve a chat model by alias via the process-level factory."""
    from models import get_factory

    return get_factory().chat(alias)


def _embedding_model(alias: str | None):
    """Resolve an embedding model by alias via the process-level factory."""
    from models import get_factory

    return get_factory().embedding(alias)


def chat(
    messages: list[dict[str, Any]],
    model: str | None = None,
    temperature: float = 0.7,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | None = None,
    **extra: Any,
) -> Any:
    """Run one blocking chat completion; returns the raw assistant *message*.

    ``model`` is now a factory *alias* (e.g. ``"gpt-4o-mini"``), not a raw
    provider model id — the underlying model id is set in ``models.yaml``. When
    ``model`` is ``None`` the factory's default chat model is used, which falls
    back to the built-in Ark adapter driven by ``config.CHAT_MODEL`` if no
    ``models.yaml`` is configured (matching P0-P5 behaviour exactly).

    ``**extra`` is forwarded to the adapter — e.g. ``thinking=True`` /
    ``top_p=0.9`` / ``extra_body={...}`` — so per-call switches ride the same
    channel without leaking provider details into the callers.
    """
    return _chat_model(model).chat(
        messages,
        temperature=temperature,
        tools=tools,
        tool_choice=tool_choice,
        **extra,
    )


def stream_chat(
    messages: list[dict[str, Any]],
    model: str | None = None,
    temperature: float = 0.7,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | None = None,
    **extra: Any,
) -> Iterator[Any]:
    """Yield SDK-shape streaming chunks (``choices[0].delta``).

    The chunk shape is the same the earlier phases already reassemble in the
    streaming ReAct loop; adapters that front OpenAI-compatible endpoints
    produce compatible objects, so no reassembly logic needs to change.
    """
    yield from _chat_model(model).stream_chat(
        messages,
        temperature=temperature,
        tools=tools,
        tool_choice=tool_choice,
        **extra,
    )


def chat_completion(
    messages: list[dict[str, Any]],
    model: str | None = None,
    temperature: float = 0.7,
    **extra: Any,
) -> str:
    """Single-turn convenience wrapper: return the reply text or ``""``.

    Used by short-lived helpers (:class:`~agents.middlewares.title.TitleMiddleware`,
    :class:`~agents.middlewares.summarization.SummarizationMiddleware`) that
    only want text, so they can stay ignorant of tool-call machinery.
    """
    message = chat(messages, model=model, temperature=temperature, **extra)
    return message.content or ""


def embed(
    inputs: list[dict[str, Any]],
    model: str | None = None,
    encoding_format: str = "float",
    **extra: Any,
) -> Any:
    """Multimodal / text embedding call, dispatched via the factory."""
    return _embedding_model(model).embed(
        inputs, encoding_format=encoding_format, **extra
    )
