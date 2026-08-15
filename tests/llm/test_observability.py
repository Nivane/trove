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
