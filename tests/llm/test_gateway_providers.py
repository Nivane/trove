"""LLMGateway provider wiring tests — litellm_params flow into litellm calls."""

from types import SimpleNamespace

from trove.core.config import ProviderConfig
from trove.llm.gateway import LLMGateway

OPENAI_PROVIDER = ProviderConfig(
    name="openai",
    litellm_params={"api_key": "k1", "api_base": "https://custom.example.com"},
)


def fake_completion(content="SELECT 1"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


class TestProviderParams:
    async def test_provider_params_passed_to_litellm(self, mocker):
        captured = {}

        async def fake_acompletion(**kwargs):
            captured.update(kwargs)
            return fake_completion()

        mocker.patch("litellm.acompletion", fake_acompletion)
        gateway = LLMGateway(providers=[OPENAI_PROVIDER])

        await gateway.chat(
            model="openai/gpt-4o",
            messages=[{"role": "user", "content": "hi"}],
        )

        assert captured["api_key"] == "k1"
        assert captured["api_base"] == "https://custom.example.com"

    async def test_explicit_args_override_provider_params(self, mocker):
        captured = {}

        async def fake_acompletion(**kwargs):
            captured.update(kwargs)
            return fake_completion()

        mocker.patch("litellm.acompletion", fake_acompletion)
        gateway = LLMGateway(providers=[OPENAI_PROVIDER])

        await gateway.chat(
            model="openai/gpt-4o",
            messages=[],
            api_key="explicit-key",
            api_base="https://other.example.com",
        )

        assert captured["api_key"] == "explicit-key"
        assert captured["api_base"] == "https://other.example.com"

    async def test_unmatched_provider_is_ignored(self, mocker):
        captured = {}

        async def fake_acompletion(**kwargs):
            captured.update(kwargs)
            return fake_completion()

        mocker.patch("litellm.acompletion", fake_acompletion)
        gateway = LLMGateway(providers=[OPENAI_PROVIDER])

        await gateway.chat(
            model="anthropic/claude-x",
            messages=[],
        )

        assert "api_key" not in captured
        assert "api_base" not in captured

    async def test_streaming_uses_provider_params(self, mocker):
        captured = {}

        class EmptyStream:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

        async def fake_acompletion(**kwargs):
            captured.update(kwargs)
            return EmptyStream()

        mocker.patch("litellm.acompletion", fake_acompletion)
        gateway = LLMGateway(providers=[OPENAI_PROVIDER])

        chunks = []
        async for chunk in gateway.chat_stream(model="openai/gpt-4o", messages=[]):
            chunks.append(chunk)

        assert chunks == []
        assert captured["api_key"] == "k1"
        assert captured["stream"] is True

    async def test_provider_without_params_works(self, mocker):
        captured = {}

        async def fake_acompletion(**kwargs):
            captured.update(kwargs)
            return fake_completion()

        mocker.patch("litellm.acompletion", fake_acompletion)
        gateway = LLMGateway(providers=[ProviderConfig(name="openai")])

        response = await gateway.chat(model="openai/gpt-4o", messages=[])
        assert response == "SELECT 1"
        assert "api_key" not in captured
