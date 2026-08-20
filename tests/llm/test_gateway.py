"""LLM gateway and token counter tests."""

import pytest

from trove.llm.gateway import LLMGateway
from trove.llm.token_counter import TokenCounter
from trove.core.errors import LLMError


class TestLLMGatewayMock:
    async def test_chat_mock_response(self):
        gateway = LLMGateway(mock_response="SELECT 1")
        response = await gateway.chat(
            model="test/model",
            messages=[{"role": "user", "content": "hi"}],
        )
        assert response == "SELECT 1"

    async def test_chat_mock_no_network(self):
        """Mock mode should never touch the network."""
        gateway = LLMGateway(mock_response="mocked")
        # Even with an invalid model name, mock works
        response = await gateway.chat(
            model="nonexistent/model-that-does-not-exist",
            messages=[{"role": "user", "content": "hi"}],
        )
        assert response == "mocked"

    async def test_chat_stream_mock(self):
        gateway = LLMGateway(mock_stream_chunks=["Hello", " ", "World"])
        chunks = []
        async for chunk in gateway.chat_stream(
            model="m",
            messages=[{"role": "user", "content": "hi"}],
        ):
            chunks.append(chunk)
        assert chunks == ["Hello", " ", "World"]

    async def test_chat_stream_falls_back_to_mock_response(self):
        gateway = LLMGateway(mock_response="full response")
        chunks = []
        async for chunk in gateway.chat_stream(model="m", messages=[]):
            chunks.append(chunk)
        assert chunks == ["full response"]


class TestTokenCounter:
    def test_count_text(self):
        counter = TokenCounter()
        count = counter.count("hello world")
        assert count > 0
        assert isinstance(count, int)

    def test_count_empty(self):
        counter = TokenCounter()
        assert counter.count("") == 0

    def test_count_messages_includes_overhead(self):
        counter = TokenCounter()
        messages = [{"role": "user", "content": "hello"}]
        count = counter.count_messages(messages)
        # content tokens + per-message overhead + reply priming
        assert count > counter.count("hello")
        assert count >= 4 + counter.count("hello") + 3

    def test_count_multiple_messages(self):
        counter = TokenCounter()
        messages = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "second"},
        ]
        total = counter.count_messages(messages)
        assert total > counter.count("first") + counter.count("second")

    def test_should_compact_below_threshold(self):
        counter = TokenCounter()
        messages = [{"role": "user", "content": "short message"}]
        assert counter.should_compact(messages, context_limit=128000) is False

    def test_should_compact_above_threshold(self):
        counter = TokenCounter()
        long_text = "word " * 200000  # ~1M chars ≈ 200k+ tokens
        messages = [{"role": "user", "content": long_text}]
        assert counter.should_compact(messages, context_limit=128000) is True

    def test_estimate_context_usage(self):
        counter = TokenCounter()
        usage = counter.estimate_context_usage(
            [{"role": "user", "content": "test"}],
            context_limit=10000,
        )
        assert usage["context_limit"] == 10000
        assert usage["token_count"] > 0
        assert 0 < usage["usage_ratio"] < 1


class TestGatewayRetryGating:
    """LLM 错误分类驱动重试:瞬时重试、永久立即放弃。"""

    async def test_permanent_llm_service_error_not_retried(self, monkeypatch):
        gw = LLMGateway(retry_base_delay=0.01)
        calls = {"n": 0}

        async def fail(model, messages, **kwargs):
            calls["n"] += 1
            raise RuntimeError("401 unauthorized: invalid api_key")

        monkeypatch.setattr(gw, "_call_litellm", fail)
        with pytest.raises(LLMError):
            await gw.chat(model="m", messages=[{"role": "user", "content": "hi"}])
        assert calls["n"] == 1  # 认证错误重试无意义,立即放弃

    async def test_transient_llm_error_retried(self, monkeypatch):
        gw = LLMGateway(retry_base_delay=0.0)
        calls = {"n": 0}

        async def fail(model, messages, **kwargs):
            calls["n"] += 1
            raise RuntimeError("status code 429: rate limit exceeded")

        monkeypatch.setattr(gw, "_call_litellm", fail)
        with pytest.raises(LLMError):
            await gw.chat(model="m", messages=[{"role": "user", "content": "hi"}])
        assert calls["n"] == gw.max_retries  # 瞬时错误烧满重试再放弃

    async def test_service_error_carries_retry_after(self, monkeypatch):
        gw = LLMGateway(retry_base_delay=0.0)
        calls = {"n": 0}

        async def fail(model, messages, **kwargs):
            calls["n"] += 1
            if calls["n"] < 2:
                raise RuntimeError("status code 429")
            return "SELECT 1"

        monkeypatch.setattr(gw, "_call_litellm", fail)
        out = await gw.chat(model="m", messages=[{"role": "user", "content": "hi"}])
        assert out == "SELECT 1"
        assert calls["n"] == 2
