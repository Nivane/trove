"""Observability wiring tests — Langfuse SDK trajectory mode."""

import pytest

from trove.llm import observability


@pytest.fixture
def no_langfuse(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)


@pytest.fixture
def langfuse_env(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")


class TestEnablement:
    def test_disabled_without_env(self, no_langfuse):
        assert observability.langfuse_enabled() is False
        assert observability.get_client() is None

    def test_enabled_with_env(self, langfuse_env):
        assert observability.langfuse_enabled() is True

    def test_handler_none_when_disabled(self, no_langfuse):
        assert observability.build_callback_handler() is None

    def test_handler_created_when_enabled(self, langfuse_env):
        assert observability.build_callback_handler() is not None

    def test_record_span_noop_when_disabled(self, no_langfuse):
        with observability.record_span("tool.execute_sql", input="SELECT 1") as span:
            assert span is None


class TestConfigureTracing:
    def test_configure_noop_without_llm_callback_registration(self, no_langfuse, monkeypatch):
        """SDK 单通道：不再注册 litellm 回调（避免双记录）。"""
        monkeypatch.setattr("litellm.success_callback", [])
        from trove.llm.tracing import configure_tracing
        from trove.core.config import TracingConfig

        configure_tracing(TracingConfig(enabled=True))
        import litellm
        assert "langfuse" not in litellm.success_callback


class _FakeObservation:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.updated = None

    def update(self, **kwargs):
        self.updated = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeV4Client:
    """只暴露 langfuse v4 的 observation API（无 v2 的 span/generation 方法）。"""

    def __init__(self):
        self.observations = []

    def start_as_current_observation(self, **kwargs):
        obs = _FakeObservation(**kwargs)
        self.observations.append(obs)
        return obs


@pytest.fixture
def fake_v4_client(langfuse_env, monkeypatch):
    client = _FakeV4Client()
    monkeypatch.setattr(observability, "_client", client)
    return client


class TestV4ApiUsage:
    """SDK v4 已移除 start_as_current_span/generation —— 必须走 observation API。"""

    def test_record_span_uses_observation_api(self, fake_v4_client):
        with observability.record_span("tool.execute_sql", input="SELECT 1") as span:
            assert span is not None
            span.update(output={"row_count": 5})
        obs = fake_v4_client.observations[0]
        assert obs.kwargs["as_type"] == "span"
        assert obs.kwargs["name"] == "tool.execute_sql"
        assert obs.kwargs["input"] == "SELECT 1"
        assert obs.updated == {"output": {"row_count": 5}}

    def test_gateway_generation_uses_observation_api(self, fake_v4_client):
        from types import SimpleNamespace
        from trove.llm import gateway as gateway_module

        response = SimpleNamespace(choices=[
            SimpleNamespace(message=SimpleNamespace(
                content="SELECT 1", reasoning_content="think",
            ))
        ])
        gateway_module._record_generation(
            "deepseek/deepseek-chat",
            [{"role": "user", "content": "hi"}],
            response,
            {"node": "gen_sql"},
        )
        obs = fake_v4_client.observations[0]
        assert obs.kwargs["as_type"] == "generation"
        assert obs.kwargs["name"] == "llm"
        assert obs.kwargs["model"] == "deepseek/deepseek-chat"
        assert obs.kwargs["output"] == {"content": "SELECT 1", "reasoning": "think"}
        assert obs.kwargs["metadata"] == {"node": "gen_sql"}

    def test_record_span_preserves_body_exception(self, fake_v4_client):
        """span 内 body 抛异常必须原样穿透——record_span 的 except 不得吞掉
        它并二次 yield(contextlib 会抛 "generator didn't stop after throw()"
        的 RuntimeError 掩盖真实错误)。"""
        import pytest

        with pytest.raises(ValueError, match="business error"):
            with observability.record_span("tool.execute_sql", input="SELECT 1"):
                raise ValueError("business error")
