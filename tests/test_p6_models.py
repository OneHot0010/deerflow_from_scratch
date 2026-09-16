"""P6 tests — multi-model factory (models/).

Offline suite: no real network / SDK calls. We exercise every layer:

- ``models.reflection.resolve_class`` — canonical / dotted / error paths.
- ``models.factory.ModelFactory`` — config parsing, lazy instantiation,
  alias resolution, default selection, capability introspection, and the
  built-in Ark fallback when no ``models.yaml`` is supplied.
- ``models.providers.ark.ArkChatModel`` / ``ArkEmbeddingModel`` — parameter
  assembly (temperature default, tool_choice default, thinking -> extra_body),
  streaming path, and delegation through the swappable client factory.
- End-to-end wiring: ``llm.chat / stream_chat / embed`` now route through the
  factory, so switching config picks a different adapter without touching call
  sites (validates the P6 roadmap acceptance: "改配置即可切换模型").
"""
from __future__ import annotations

import pytest

import llm
import models
from models.base import BaseChatModel, BaseEmbeddingModel, ModelCapabilities
from models.factory import ModelConfigError, ModelFactory, load_config
from models.providers import ark as ark_provider
from models.providers.ark import ArkChatModel, ArkEmbeddingModel
from models.reflection import ClassResolutionError, resolve_class


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------
class _FakeMessage:
    def __init__(self, content: str):
        self.content = content
        self.tool_calls = None


class _FakeCompletion:
    def __init__(self, message):
        self.choices = [type("C", (), {"message": message})()]


class _FakeChatCompletions:
    def __init__(self, ret):
        self._ret = ret
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._ret


class _FakeEmbeddings:
    def __init__(self):
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return {"data": [{"embedding": [0.1]}]}


class _FakeArk:
    def __init__(self, ret):
        self.chat = type("Ns", (), {"completions": _FakeChatCompletions(ret)})()
        self.multimodal_embeddings = _FakeEmbeddings()


# ---------------------------------------------------------------------------
# reflection
# ---------------------------------------------------------------------------
class TestReflection:
    def test_canonical_colon_path_resolves(self):
        cls = resolve_class("models.providers.ark:ArkChatModel")
        assert cls is ArkChatModel

    def test_dotted_path_resolves_as_fallback(self):
        cls = resolve_class("models.providers.ark.ArkChatModel")
        assert cls is ArkChatModel

    def test_missing_module_reports_actionable_error(self):
        with pytest.raises(ClassResolutionError, match="cannot import module"):
            resolve_class("no.such.pkg:Cls")

    def test_missing_attribute_reports_actionable_error(self):
        with pytest.raises(ClassResolutionError, match="has no attribute"):
            resolve_class("models.providers.ark:DoesNotExist")

    def test_empty_or_malformed_path(self):
        with pytest.raises(ClassResolutionError):
            resolve_class("")
        with pytest.raises(ClassResolutionError):
            resolve_class("no_dots_or_colons")


# ---------------------------------------------------------------------------
# ArkChatModel adapter
# ---------------------------------------------------------------------------
class TestArkChatModel:
    @pytest.fixture
    def fake_client(self):
        fake = _FakeArk(_FakeCompletion(_FakeMessage("ok")))
        ark_provider.set_client_factory(lambda: fake)
        yield fake
        ark_provider.set_client_factory(None)

    def test_chat_forwards_messages_model_and_defaults(self, fake_client):
        m = ArkChatModel(model="doubao-x")
        out = m.chat([{"role": "user", "content": "hi"}])
        assert out.content == "ok"
        sent = fake_client.chat.completions.calls[0]
        assert sent["model"] == "doubao-x"
        assert sent["messages"] == [{"role": "user", "content": "hi"}]
        assert sent["temperature"] == 0.7
        assert "tools" not in sent and "tool_choice" not in sent
        assert "stream" not in sent

    def test_tools_default_to_auto_tool_choice(self, fake_client):
        m = ArkChatModel(model="doubao-x")
        tools = [{"type": "function", "function": {"name": "ping", "parameters": {}}}]
        m.chat([{"role": "user", "content": "hi"}], tools=tools)
        sent = fake_client.chat.completions.calls[0]
        assert sent["tools"] == tools
        assert sent["tool_choice"] == "auto"

    def test_thinking_toggle_lands_in_extra_body(self, fake_client):
        m = ArkChatModel(model="doubao-x", thinking=True)
        m.chat([{"role": "user", "content": "hi"}])
        sent = fake_client.chat.completions.calls[0]
        assert sent["extra_body"] == {"thinking": {"type": "enabled"}}

    def test_per_call_thinking_overrides_default(self, fake_client):
        m = ArkChatModel(model="doubao-x", thinking=False)
        m.chat([{"role": "user", "content": "hi"}], thinking=True)
        sent = fake_client.chat.completions.calls[0]
        assert sent["extra_body"] == {"thinking": {"type": "enabled"}}

    def test_capabilities_recorded_from_ctor(self):
        m = ArkChatModel(model="doubao-x", thinking=True, vision=True)
        assert m.capabilities == ModelCapabilities(thinking=True, vision=True, streaming=True)

    def test_stream_chat_sets_stream_and_iterates_chunks(self):
        chunks = ["a", "b", "c"]
        fake = _FakeArk(iter(chunks))
        ark_provider.set_client_factory(lambda: fake)
        try:
            m = ArkChatModel(model="doubao-x")
            got = list(m.stream_chat([{"role": "user", "content": "hi"}]))
            assert got == chunks
            assert fake.chat.completions.calls[0]["stream"] is True
        finally:
            ark_provider.set_client_factory(None)

    def test_extra_kwargs_passthrough(self, fake_client):
        m = ArkChatModel(model="doubao-x")
        m.chat([{"role": "user", "content": "hi"}], top_p=0.5, seed=42)
        sent = fake_client.chat.completions.calls[0]
        assert sent["top_p"] == 0.5 and sent["seed"] == 42


class TestArkEmbeddingModel:
    def test_embed_calls_multimodal_endpoint(self):
        fake = _FakeArk(_FakeCompletion(_FakeMessage("")))
        ark_provider.set_client_factory(lambda: fake)
        try:
            m = ArkEmbeddingModel(model="emb-x")
            inputs = [{"type": "text", "text": "hi"}]
            m.embed(inputs)
            call = fake.multimodal_embeddings.calls[0]
            assert call["model"] == "emb-x"
            assert call["input"] == inputs
            assert call["encoding_format"] == "float"
        finally:
            ark_provider.set_client_factory(None)


# ---------------------------------------------------------------------------
# ModelFactory
# ---------------------------------------------------------------------------
class TestFactoryConfig:
    def test_empty_config_falls_back_to_ark_default(self):
        f = ModelFactory({})
        assert not f.has_config()
        chat = f.chat()
        assert isinstance(chat, ArkChatModel)
        # Same instance on repeated access (cached).
        assert f.chat() is chat

    def test_default_chat_alias_resolves(self):
        cfg = {
            "default_chat": "primary",
            "chat": {
                "primary": {
                    "class": "models.providers.ark:ArkChatModel",
                    "params": {"model": "doubao-x", "thinking": True},
                }
            },
        }
        f = ModelFactory(cfg)
        m = f.chat()
        assert isinstance(m, ArkChatModel)
        assert m.model == "doubao-x"
        assert m.thinking is True
        assert m.name == "primary"  # inherited from alias when unspecified
        assert f.capabilities().thinking is True

    def test_named_alias_resolves(self):
        cfg = {
            "chat": {
                "a": {"class": "models.providers.ark:ArkChatModel", "params": {"model": "m-a"}},
                "b": {"class": "models.providers.ark:ArkChatModel", "params": {"model": "m-b"}},
            }
        }
        f = ModelFactory(cfg)
        assert f.chat("a").model == "m-a"
        assert f.chat("b").model == "m-b"

    def test_unknown_alias_raises(self):
        f = ModelFactory({"chat": {"a": {"class": "models.providers.ark:ArkChatModel", "params": {"model": "x"}}}})
        with pytest.raises(ModelConfigError, match="unknown chat model alias"):
            f.chat("ghost")

    def test_bad_class_path_bubbles_up(self):
        f = ModelFactory({"chat": {"a": {"class": "nope:Cls", "params": {"model": "x"}}}})
        with pytest.raises(ClassResolutionError):
            f.chat("a")

    def test_missing_class_key_is_configerror(self):
        f = ModelFactory({"chat": {"a": {"params": {"model": "x"}}}})
        with pytest.raises(ModelConfigError, match="missing required key 'class'"):
            f.chat("a")

    def test_wrong_base_class_is_rejected(self):
        # dict is not a BaseChatModel subclass -> the factory must reject it.
        f = ModelFactory({"chat": {"a": {"class": "builtins:dict", "params": {}}}})
        with pytest.raises(ModelConfigError, match="not a BaseChatModel subclass"):
            f.chat("a")

    def test_embedding_alias_resolves_and_caches(self):
        cfg = {
            "default_embedding": "e",
            "embedding": {
                "e": {"class": "models.providers.ark:ArkEmbeddingModel", "params": {"model": "emb-x"}}
            },
        }
        f = ModelFactory(cfg)
        e1 = f.embedding()
        e2 = f.embedding("e")
        assert isinstance(e1, ArkEmbeddingModel) and e1 is e2

    def test_single_entry_becomes_implicit_default(self):
        # No `default_chat` key but exactly one entry -> factory picks it.
        cfg = {"chat": {"solo": {"class": "models.providers.ark:ArkChatModel", "params": {"model": "m"}}}}
        f = ModelFactory(cfg)
        assert f.chat().model == "m"

    def test_list_helpers(self):
        cfg = {
            "chat": {"a": {"class": "models.providers.ark:ArkChatModel", "params": {"model": "m"}}},
            "embedding": {"e": {"class": "models.providers.ark:ArkEmbeddingModel", "params": {"model": "m"}}},
        }
        f = ModelFactory(cfg)
        assert f.list_chat() == ["a"]
        assert f.list_embedding() == ["e"]


class TestFactoryYAMLLoad:
    def test_load_yaml_from_disk(self, tmp_path):
        cfg_file = tmp_path / "models.yaml"
        cfg_file.write_text(
            "default_chat: main\n"
            "chat:\n"
            "  main:\n"
            "    class: models.providers.ark:ArkChatModel\n"
            "    params:\n"
            "      model: doubao-loaded\n"
            "      thinking: true\n",
            encoding="utf-8",
        )
        loaded = load_config(cfg_file)
        assert loaded is not None
        f = ModelFactory(loaded)
        m = f.chat()
        assert m.model == "doubao-loaded"
        assert m.thinking is True

    def test_load_missing_file_returns_none(self, tmp_path):
        assert load_config(tmp_path / "does-not-exist.yaml") is None

    def test_env_override_points_at_config(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / "custom.yaml"
        cfg_file.write_text(
            "chat:\n"
            "  only:\n"
            "    class: models.providers.ark:ArkChatModel\n"
            "    params:\n"
            "      model: env-selected\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("MODELS_CONFIG", str(cfg_file))
        # get_factory() lazily reads MODELS_CONFIG on first call.
        models.set_factory(None)
        f = models.get_factory()
        assert f.chat().model == "env-selected"


class TestProcessLevelAccessor:
    def test_get_factory_is_lazy_singleton(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MODELS_CONFIG", str(tmp_path / "absent.yaml"))
        models.set_factory(None)
        f1 = models.get_factory()
        f2 = models.get_factory()
        assert f1 is f2

    def test_set_factory_replaces_singleton(self):
        f = ModelFactory({"chat": {"only": {"class": "models.providers.ark:ArkChatModel", "params": {"model": "x"}}}})
        models.set_factory(f)
        assert models.get_factory() is f


# ---------------------------------------------------------------------------
# End-to-end: llm.py routes through the factory
# ---------------------------------------------------------------------------
class TestLLMRoutesThroughFactory:
    """Verify that changing the factory's config swaps behaviour end-to-end."""

    def _install_recorder(self, monkeypatch):
        recorded: dict[str, list] = {"chat": [], "stream": [], "embed": []}

        class _RecordingChat(BaseChatModel):
            def __init__(self, tag):
                self.tag = tag
                self.name = tag
                self.capabilities = ModelCapabilities()

            def chat(self, messages, **kw):
                recorded["chat"].append({"tag": self.tag, "messages": messages, "kw": kw})
                return _FakeMessage(f"from-{self.tag}")

            def stream_chat(self, messages, **kw):
                recorded["stream"].append({"tag": self.tag, "messages": messages, "kw": kw})
                # yield one dummy chunk
                yield f"chunk-{self.tag}"

        class _RecordingEmbed(BaseEmbeddingModel):
            def __init__(self, tag):
                self.tag = tag
                self.name = tag

            def embed(self, inputs, **kw):
                recorded["embed"].append({"tag": self.tag, "inputs": inputs, "kw": kw})
                return {"data": [{"embedding": [1.0]}]}

        # Register the recorders as importable classes for reflection.
        import sys
        this = sys.modules[__name__]
        this._RecordingChat = _RecordingChat
        this._RecordingEmbed = _RecordingEmbed

        cfg = {
            "default_chat": "primary",
            "default_embedding": "primary",
            "chat": {
                "primary": {"class": f"{__name__}:_RecordingChat", "params": {"tag": "PRIMARY"}},
                "secondary": {"class": f"{__name__}:_RecordingChat", "params": {"tag": "SECONDARY"}},
            },
            "embedding": {
                "primary": {"class": f"{__name__}:_RecordingEmbed", "params": {"tag": "EMB"}}
            },
        }
        models.set_factory(ModelFactory(cfg))
        return recorded

    def test_chat_defaults_to_primary(self, monkeypatch):
        recorded = self._install_recorder(monkeypatch)
        out = llm.chat([{"role": "user", "content": "hi"}])
        assert out.content == "from-PRIMARY"
        assert recorded["chat"][0]["tag"] == "PRIMARY"

    def test_chat_switches_by_alias(self, monkeypatch):
        recorded = self._install_recorder(monkeypatch)
        out = llm.chat([{"role": "user", "content": "hi"}], model="secondary")
        assert out.content == "from-SECONDARY"
        assert recorded["chat"][-1]["tag"] == "SECONDARY"

    def test_chat_completion_returns_text(self, monkeypatch):
        self._install_recorder(monkeypatch)
        assert llm.chat_completion([{"role": "user", "content": "hi"}]) == "from-PRIMARY"

    def test_stream_chat_yields_chunks(self, monkeypatch):
        recorded = self._install_recorder(monkeypatch)
        out = list(llm.stream_chat([{"role": "user", "content": "hi"}], model="secondary"))
        assert out == ["chunk-SECONDARY"]
        assert recorded["stream"][-1]["tag"] == "SECONDARY"

    def test_extra_kwargs_pass_through(self, monkeypatch):
        recorded = self._install_recorder(monkeypatch)
        llm.chat([{"role": "user", "content": "hi"}], thinking=True, top_p=0.3)
        kw = recorded["chat"][-1]["kw"]
        assert kw["thinking"] is True and kw["top_p"] == 0.3

    def test_embed_dispatches_to_embedding_alias(self, monkeypatch):
        recorded = self._install_recorder(monkeypatch)
        llm.embed([{"type": "text", "text": "hi"}])
        assert recorded["embed"][-1]["tag"] == "EMB"


# ---------------------------------------------------------------------------
# Backward-compat: llm._client still works and P0-P5 code paths hit Ark
# ---------------------------------------------------------------------------
class TestBackwardCompatibility:
    def test_llm_client_hook_still_exists(self):
        # The lru_cached _client attribute is what the P0 tests patch.
        assert callable(llm._client)
        assert hasattr(llm._client, "cache_clear")

    def test_ark_provider_reuses_llm_client(self, monkeypatch):
        """When no models.yaml is loaded, the Ark fallback must consult
        ``llm._client`` — this is what makes the P0 test suite keep passing.
        """
        fake = _FakeArk(_FakeCompletion(_FakeMessage("bwd-compat")))
        monkeypatch.setattr(llm, "_client", lambda: fake)
        # Fresh factory with no config -> fallback Ark model.
        models.set_factory(ModelFactory({}))
        out = llm.chat([{"role": "user", "content": "hi"}])
        assert out.content == "bwd-compat"
