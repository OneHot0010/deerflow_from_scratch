"""Model provider contracts (P6).

Two tiny abstract base classes every provider adapter implements. Both mirror
the OpenAI-style protocol that the earlier phases already speak, so the
adapter surface is a straight rename of the existing ``llm.py`` API — nothing
above this layer has to change when the underlying provider does.

- :class:`BaseChatModel` — ``chat`` (blocking) + ``stream_chat`` (streaming).
  Both accept ``tools`` (OpenAI/Ark-style array), ``tool_choice`` and free-form
  kwargs. The kwargs are how DeerFlow's ``thinking`` / ``vision`` switches
  travel end-to-end without polluting every layer with model-specific
  parameters — adapters cherry-pick what applies to them and drop the rest.

- :class:`BaseEmbeddingModel` — ``embed`` for the multimodal / text embedding
  endpoint. Kept separate so a config can wire chat and embedding to different
  providers (e.g. chat via Claude, embeddings via Ark).

The tiny :class:`ModelCapabilities` dataclass records what the model *actually*
supports — the factory reads it back so callers (or middlewares) can decide
whether to enable thinking / vision on a per-call basis without hard-coding
per-provider knowledge.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Iterator


@dataclass(frozen=True)
class ModelCapabilities:
    """Static description of what a chat model supports.

    Read by the factory (:meth:`ModelFactory.capabilities`) and forwarded to
    callers that need to decide whether to pass ``thinking=True`` /
    multi-part vision content on this turn.
    """

    thinking: bool = False
    """Whether the model exposes an explicit reasoning / thinking channel."""

    vision: bool = False
    """Whether the model accepts image parts in ``content``."""

    streaming: bool = True
    """Whether ``stream_chat`` is implemented (always True for our built-ins)."""


class BaseChatModel(ABC):
    """The one shape the ReAct loop / SSE server / middlewares talk to."""

    #: Human-friendly identifier used in errors / logs. Set by ``__init__``.
    name: str = ""
    #: Static capability flags. Overridden by subclasses via ``__init__``.
    capabilities: ModelCapabilities = ModelCapabilities()

    @abstractmethod
    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.7,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        **extra: Any,
    ) -> Any:
        """Run one blocking chat completion and return the assistant *message*.

        The return value must expose ``.content`` (str | None) and, when the
        model emitted tool calls, ``.tool_calls`` (each with ``.id`` and a
        ``.function.name`` / ``.function.arguments``). This is exactly what the
        blocking ReAct loop appends back into the running conversation.
        """

    @abstractmethod
    def stream_chat(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.7,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        **extra: Any,
    ) -> Iterator[Any]:
        """Yield SDK-shape delta chunks (``choices[0].delta`` with content
        and/or ``tool_calls``). The streaming ReAct loop reassembles them."""


class BaseEmbeddingModel(ABC):
    """Multimodal / text embedding backend."""

    name: str = ""

    @abstractmethod
    def embed(
        self,
        inputs: list[dict[str, Any]],
        *,
        encoding_format: str = "float",
        **extra: Any,
    ) -> Any:
        """Return the raw SDK embedding response (``.data[].embedding``)."""
