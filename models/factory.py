"""ModelFactory — turn ``models.yaml`` into ready-to-use model instances (P6).

Config shape (see ``models.example.yaml``)::

    default_chat: main
    default_embedding: emb
    chat:
      main:
        class: models.providers.ark:ArkChatModel
        params:
          model: doubao-seed-1-6-250615
          thinking: true
      claude:
        class: models.providers.openai:OpenAIChatModel
        params:
          model: claude-3-5-sonnet
          base_url: https://api.anthropic.com/v1
          api_key_env: ANTHROPIC_API_KEY
    embedding:
      emb:
        class: models.providers.ark:ArkEmbeddingModel
        params:
          model: doubao-embedding-vision-251215

Design notes:

- **Reflection everywhere.** Providers are declared by fully qualified class
  path, resolved by :func:`models.reflection.resolve_class`. No provider table
  in the factory; users can drop a third-party adapter into their own package
  and reference it from ``models.yaml``.
- **Lazy instantiation.** ``chat("alias")`` / ``embedding("alias")`` only
  builds the instance the first time it is asked for, and caches it. Loading a
  huge config with 20 unused aliases never pays for their client bootstrap.
- **Sensible fallback.** If no config is provided (or the file is missing) the
  factory returns a built-in Ark chat + embedding pair driven by
  ``config.CHAT_MODEL`` / ``config.EMBEDDING_MODEL`` so P0-P5 behaviour is
  identical without a ``models.yaml``.
- **Process-level accessor.** :func:`get_factory` / :func:`set_factory` mirror
  the pattern used elsewhere in the project (thread store, sandbox provider)
  so tests can inject a controlled factory.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from models.base import BaseChatModel, BaseEmbeddingModel, ModelCapabilities
from models.reflection import resolve_class


class ModelConfigError(RuntimeError):
    """Raised when ``models.yaml`` is malformed or references unknown aliases."""


# ---------------------------------------------------------------------------
# config loading
# ---------------------------------------------------------------------------
def _default_config_path() -> Path:
    """Return the on-disk path where the factory looks for ``models.yaml``.

    Overridable via the ``MODELS_CONFIG`` env var so ops can point at a
    per-environment file without editing source. Defaults to a ``models.yaml``
    sitting next to the source tree.
    """
    override = os.environ.get("MODELS_CONFIG")
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parent.parent / "models.yaml"


def load_config(path: str | os.PathLike | None = None) -> dict[str, Any] | None:
    """Read ``models.yaml`` if it exists. Returns ``None`` for a missing file.

    We prefer PyYAML when available (the whole config is a small mapping so no
    streaming parser is needed) and fall back to a very small hand-rolled
    parser that handles the flat two-level shape our example uses — that keeps
    ``import models`` from hard-requiring PyYAML in environments that have not
    installed it yet.
    """
    target = Path(path) if path is not None else _default_config_path()
    if not target.exists():
        return None
    text = target.read_text(encoding="utf-8")

    try:
        import yaml  # type: ignore

        return yaml.safe_load(text) or {}
    except ImportError:  # pragma: no cover - PyYAML is in requirements.txt
        return _tiny_yaml_load(text)


def _tiny_yaml_load(text: str) -> dict[str, Any]:
    """Fallback loader for the exact shape the P6 example uses.

    Not a general YAML parser — just enough to keep the factory importable in
    an environment where PyYAML has not yet been installed. Real deployments
    should use PyYAML (already declared in requirements.txt).
    """
    root: dict[str, Any] = {}
    stack: list[tuple[int, Any]] = [(-1, root)]
    for raw_line in text.splitlines():
        # strip comments and blank lines
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        # pop back to the correct nesting level
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1] if stack else root
        key, sep, value = line.strip().partition(":")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if value == "":
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            if value.lower() in {"true", "false"}:
                parent[key] = value.lower() == "true"
            elif value.lstrip("-").isdigit():
                parent[key] = int(value)
            else:
                parent[key] = value.strip("'\"")
    return root


# ---------------------------------------------------------------------------
# factory
# ---------------------------------------------------------------------------
class ModelFactory:
    """Instantiate chat / embedding models by alias, driven by config.

    The factory holds only the *config* dict; instances are built lazily and
    cached on first access. Rebuilding a factory (``ModelFactory(new_cfg)``) is
    cheap and completely resets the cache — handy when config is hot-reloaded.
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config: dict[str, Any] = config or {}
        self._chat_cache: dict[str, BaseChatModel] = {}
        self._embedding_cache: dict[str, BaseEmbeddingModel] = {}

    # -- introspection ------------------------------------------------------
    def has_config(self) -> bool:
        """True iff an explicit ``models.yaml`` (or dict) was supplied."""
        return bool(self.config)

    def default_chat_alias(self) -> str:
        """Alias used when the caller does not name one."""
        alias = self.config.get("default_chat")
        if alias:
            return alias
        # No default declared but exactly one chat model listed? Use it.
        chats = self.config.get("chat") or {}
        if len(chats) == 1:
            return next(iter(chats))
        return ""

    def default_embedding_alias(self) -> str:
        alias = self.config.get("default_embedding")
        if alias:
            return alias
        embs = self.config.get("embedding") or {}
        if len(embs) == 1:
            return next(iter(embs))
        return ""

    def list_chat(self) -> list[str]:
        return list((self.config.get("chat") or {}).keys())

    def list_embedding(self) -> list[str]:
        return list((self.config.get("embedding") or {}).keys())

    def capabilities(self, alias: str | None = None) -> ModelCapabilities:
        """Return the capability flags of a chat model."""
        return self.chat(alias).capabilities

    # -- lookup -------------------------------------------------------------
    def chat(self, alias: str | None = None) -> BaseChatModel:
        """Return the chat model registered under ``alias`` (or the default)."""
        alias = alias or self.default_chat_alias()
        if not alias:
            # No config at all -> fall back to the P0 defaults (Ark).
            return self._fallback_chat()

        if alias in self._chat_cache:
            return self._chat_cache[alias]

        spec = (self.config.get("chat") or {}).get(alias)
        if spec is None:
            raise ModelConfigError(
                f"ModelFactory: unknown chat model alias {alias!r}. "
                f"Known aliases: {self.list_chat() or '(none)'}"
            )
        instance = self._instantiate(spec, expected_base=BaseChatModel, alias=alias)
        # If the caller didn't pin an explicit ``name`` in params, use the alias
        # so the alias is the visible identity, not the raw model id.
        if not (isinstance(spec, dict) and (spec.get("params") or {}).get("name")):
            instance.name = alias
        self._chat_cache[alias] = instance
        return instance

    def embedding(self, alias: str | None = None) -> BaseEmbeddingModel:
        """Return the embedding model registered under ``alias`` (or the default)."""
        alias = alias or self.default_embedding_alias()
        if not alias:
            return self._fallback_embedding()

        if alias in self._embedding_cache:
            return self._embedding_cache[alias]

        spec = (self.config.get("embedding") or {}).get(alias)
        if spec is None:
            raise ModelConfigError(
                f"ModelFactory: unknown embedding model alias {alias!r}. "
                f"Known aliases: {self.list_embedding() or '(none)'}"
            )
        instance = self._instantiate(spec, expected_base=BaseEmbeddingModel, alias=alias)
        if not (isinstance(spec, dict) and (spec.get("params") or {}).get("name")):
            instance.name = alias
        self._embedding_cache[alias] = instance
        return instance

    # -- internals ----------------------------------------------------------
    def _instantiate(
        self,
        spec: dict[str, Any],
        *,
        expected_base: type,
        alias: str,
    ) -> Any:
        """Resolve ``spec['class']`` and call it with ``spec['params']``."""
        if not isinstance(spec, dict):
            raise ModelConfigError(
                f"ModelFactory: entry for {alias!r} must be a mapping, got {type(spec).__name__}"
            )
        class_path = spec.get("class")
        if not class_path:
            raise ModelConfigError(
                f"ModelFactory: entry for {alias!r} is missing required key 'class'"
            )
        cls = resolve_class(class_path)
        params = spec.get("params") or {}
        if not isinstance(params, dict):
            raise ModelConfigError(
                f"ModelFactory: 'params' for {alias!r} must be a mapping"
            )
        instance = cls(**params)
        if not isinstance(instance, expected_base):
            raise ModelConfigError(
                f"ModelFactory: {class_path!r} produced {type(instance).__name__}, "
                f"which is not a {expected_base.__name__} subclass"
            )
        return instance

    def _fallback_chat(self) -> BaseChatModel:
        """Built-in Ark chat model, matches P0-P5 behaviour byte-for-byte."""
        if "__fallback__" not in self._chat_cache:
            from models.providers.ark import ArkChatModel
            import config as app_config

            self._chat_cache["__fallback__"] = ArkChatModel(
                model=app_config.CHAT_MODEL, name="ark-default"
            )
        return self._chat_cache["__fallback__"]

    def _fallback_embedding(self) -> BaseEmbeddingModel:
        """Built-in Ark embedding model."""
        if "__fallback__" not in self._embedding_cache:
            from models.providers.ark import ArkEmbeddingModel
            import config as app_config

            self._embedding_cache["__fallback__"] = ArkEmbeddingModel(
                model=app_config.EMBEDDING_MODEL, name="ark-embedding-default"
            )
        return self._embedding_cache["__fallback__"]


# ---------------------------------------------------------------------------
# process-level accessor
# ---------------------------------------------------------------------------
_factory: ModelFactory | None = None


def get_factory() -> ModelFactory:
    """Return (lazily building) the process-level ``ModelFactory``.

    The first call loads ``models.yaml`` (or the file pointed at by the
    ``MODELS_CONFIG`` env var). Subsequent calls reuse the same instance so all
    of the app (llm.py, middlewares, subagents in P9) share one alias table
    and one cache of instantiated clients.
    """
    global _factory
    if _factory is None:
        _factory = ModelFactory(load_config())
    return _factory


def set_factory(factory: ModelFactory | None) -> None:
    """Install (or clear) the process-level factory. Used by tests."""
    global _factory
    _factory = factory
