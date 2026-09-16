"""models — P6 multi-model factory.

DeerFlow's ``models/factory.py`` + ``reflection/`` layer lets the framework
declare arbitrary chat / embedding backends in ``config.yaml`` and swap between
them at runtime without touching call sites. This package is the minimal,
zero-new-dependency counterpart.

Layout:

- ``base.py``         — ``BaseChatModel`` / ``BaseEmbeddingModel`` contracts
  every provider adapter implements. Speaks the OpenAI-style
  function-calling protocol the earlier phases already assume, so the
  ReAct loop, SSE streaming and P4 middlewares keep working unchanged.
- ``reflection.py``   — ``resolve_class("pkg.mod:Class")`` — the tiny reflection
  helper that lets ``models.yaml`` declare provider classes by string and the
  factory instantiate them without a hard-coded provider table.
- ``factory.py``      — ``ModelFactory`` (config -> named model instances) and
  the process-level accessors ``get_factory()`` / ``set_factory()``.
- ``providers/ark.py``    — Ark (Doubao) provider adapter. Keeps calling
  ``llm._client()`` under the hood so tests that patch that entry point stay
  green and P0-P5 behaviour is byte-for-byte unchanged.
- ``providers/openai.py`` — OpenAI-compatible provider adapter, generic enough
  to also front DeepSeek / Together / any endpoint that speaks the OpenAI
  Chat Completions protocol via ``base_url``.

The ``llm.py`` module now delegates to whichever chat/embedding model the
factory hands back for the requested alias (defaulting to the built-in Ark
provider when no ``models.yaml`` is present). That single point of change is
exactly what the roadmap's P6 milestone calls for — "改配置即可切换模型".
"""
from __future__ import annotations

from models.base import BaseChatModel, BaseEmbeddingModel, ModelCapabilities
from models.factory import ModelFactory, get_factory, load_config, set_factory
from models.reflection import resolve_class

__all__ = [
    "BaseChatModel",
    "BaseEmbeddingModel",
    "ModelCapabilities",
    "ModelFactory",
    "get_factory",
    "load_config",
    "set_factory",
    "resolve_class",
]
