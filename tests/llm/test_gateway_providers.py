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

    async def test_metadata_passed_to_litellm(self, mocker):
        """trace metadata（session/node）进入 litellm 调用。"""
        captured = {}

        async def fake_acompletion(**kwargs):
            captured.update(kwargs)
            return fake_completion()

        mocker.patch("litellm.acompletion", fake_acompletion)
        gateway = LLMGateway()

        await gateway.chat(
            model="openai/gpt-4o",
            messages=[],
            metadata={"node": "gen_sql", "session_id": "s1"},
        )
        assert captured["metadata"] == {"node": "gen_sql", "session_id": "s1"}

    async def test_generation_recorded_when_tracing_enabled(self, mocker):
        """SDK 轨迹：litellm 响应后记录 generation（含 reasoning_content/CoT）。"""
        captured = {}

        class FakeGeneration:
            def __init__(self, name, model, input, output, metadata):
                captured.update(
                    {"name": name, "model": model, "input": input,
                     "output": output, "metadata": metadata},
                )

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class FakeClient:
            def start_as_current_generation(self, **kwargs):
                return FakeGeneration(**kwargs)

        import trove.llm.gateway as gateway_module
        mocker.patch.object(gateway_module, "get_client", return_value=FakeClient())

        async def fake_acompletion(**kwargs):
            resp = fake_completion()
            resp.choices[0].message.reasoning_content = "step by step reasoning"
            return resp

        mocker.patch("litellm.acompletion", fake_acompletion)
        gateway = LLMGateway()

        await gateway.chat(
            model="deepseek/deepseek-reasoner",
            messages=[{"role": "user", "content": "q"}],
            metadata={"node": "planner"},
        )
        assert captured["name"] == "llm"
        assert captured["model"] == "deepseek/deepseek-reasoner"
        assert "step by step reasoning" in captured["output"]["reasoning"]
        assert captured["metadata"] == {"node": "planner"}

    async def test_no_generation_when_disabled(self, mocker):
        """未配置 Langfuse 时，SDK 记录静默跳过。"""
        import trove.llm.gateway as gateway_module
        mocker.patch.object(gateway_module, "get_client", return_value=None)

        async def fake_acompletion(**kwargs):
            return fake_completion()

        mocker.patch("litellm.acompletion", fake_acompletion)
        gateway = LLMGateway()

        response = await gateway.chat(model="openai/gpt-4o", messages=[])
        assert response == "SELECT 1"

    async def test_chat_full_returns_message_with_tool_calls(self, mocker):
        """chat_full 返回完整 message（content + tool_calls），tools 透传。"""
        from types import SimpleNamespace

        captured = {}

        async def fake_acompletion(**kwargs):
            captured.update(kwargs)
            tool_call = SimpleNamespace(
                id="call_1",
                type="function",
                function=SimpleNamespace(name="execute_sql", arguments='{"sql": "SELECT 1"}'),
            )
            message = SimpleNamespace(content=None, tool_calls=[tool_call])
            message.reasoning_content = None
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

        mocker.patch("litellm.acompletion", fake_acompletion)
        gateway = LLMGateway()

        full = await gateway.chat_full(
            model="openai/gpt-4o",
            messages=[],
            tools=[{"type": "function", "function": {"name": "execute_sql", "parameters": {}}}],
        )
        assert captured["tools"][0]["function"]["name"] == "execute_sql"
        assert full["tool_calls"][0]["name"] == "execute_sql"
        assert full["tool_calls"][0]["arguments"] == '{"sql": "SELECT 1"}'

    async def test_chat_full_content_only(self, mocker):
        from types import SimpleNamespace

        async def fake_acompletion(**kwargs):
            message = SimpleNamespace(content="final answer", tool_calls=None)
            message.reasoning_content = None
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

        mocker.patch("litellm.acompletion", fake_acompletion)
        gateway = LLMGateway()

        full = await gateway.chat_full(model="m", messages=[])
        assert full["content"] == "final answer"
        assert full["tool_calls"] == []
