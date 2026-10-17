"""网关 provider/model 白名单(egress)单测。

钉死的边界:

- 空白名单 / ``*`` / 关闭开关 → 全部允许(老部署行为不变);
- 含 ``/`` 的条目 = 精确模型;**不含** ``/`` = provider 前缀;
- 拒绝发生在**任何 provider 调用之前**(计数器为 0),mock 短路仍最先;
- 策略 callable 现读(管理端热改下一次调用生效);读策略失败 → fail-closed;
- 指标只带 provider(基数有界),错误信息不回显 prompt。
"""

from __future__ import annotations

import litellm
import pytest

from trove.core.config import EgressConfig
from trove.core.metrics import render_metrics
from trove.llm.gateway import LLMEgressDenied, LLMGateway

MESSAGES = [{"role": "user", "content": "hi"}]
SECRET_PROMPT = "PROMPT-SECRET-9f3a"


class _Contacted(Exception):
    pass


def _gateway(policy=None, **kwargs) -> LLMGateway:
    return LLMGateway(
        max_retries=1, retry_base_delay=0, egress_policy=policy, **kwargs,
    )


async def _invoke(gateway: LLMGateway, kind: str, model: str):
    if kind == "chat":
        return await gateway.chat(model=model, messages=MESSAGES)
    if kind == "chat_full":
        return await gateway.chat_full(model=model, messages=MESSAGES)
    if kind == "chat_stream":
        return [c async for c in gateway.chat_stream(model=model, messages=MESSAGES)]
    if kind == "embedding":
        return await gateway.embedding(model=model, inputs=["x"])
    raise AssertionError(kind)


@pytest.fixture
def contact_counter(monkeypatch):
    """统计真正到达 provider 的调用数。

    四条入口(chat / chat_full / chat_stream / embedding)最终都落在
    ``litellm.acompletion`` / ``litellm.aembedding``;探针在那里截住,
    以计数器为 0 断言「拒绝不触网」。
    """
    calls: list[str] = []

    async def _acompletion(**kwargs):
        calls.append(kwargs.get("model", ""))
        raise _Contacted()

    async def _aembedding(**kwargs):
        calls.append(kwargs.get("model", ""))
        raise _Contacted()

    monkeypatch.setattr(litellm, "acompletion", _acompletion)
    monkeypatch.setattr(litellm, "aembedding", _aembedding)
    return calls


async def _assert_contacts(gateway: LLMGateway, counter, kind, model):
    """允许方向:请求确实到达 provider(被探针以 _Contacted 截住)。"""
    with pytest.raises((_Contacted, Exception)) as ei:
        await _invoke(gateway, kind, model)
    assert not isinstance(ei.value, LLMEgressDenied)
    assert counter, f"{kind} 未联系 provider"


async def _assert_denied(gateway: LLMGateway, counter, kind, model):
    with pytest.raises(LLMEgressDenied):
        await _invoke(gateway, kind, model)
    assert counter == [], f"{kind} 拒绝后仍联系了 provider"


# ── 允许方向 ────────────────────────────────────────────────


class TestAllowed:
    async def test_no_policy_allows(self, contact_counter):
        gw = _gateway()
        await _assert_contacts(gw, contact_counter, "chat", "openai/gpt-4o")

    async def test_empty_allowlist_allows(self, contact_counter):
        gw = _gateway(EgressConfig())
        await _assert_contacts(gw, contact_counter, "chat_full", "anything/at-all")

    async def test_star_allows(self, contact_counter):
        gw = _gateway(EgressConfig(allow_providers=["*"]))
        await _assert_contacts(gw, contact_counter, "chat", "deepseek/deepseek-chat")

    async def test_provider_prefix_allows(self, contact_counter):
        gw = _gateway(EgressConfig(allow_providers=["openai"]))
        for kind in ("chat", "chat_full", "chat_stream", "embedding"):
            await _assert_contacts(gw, contact_counter, kind, "openai/gpt-4o")

    async def test_exact_model_entry_allows(self, contact_counter):
        gw = _gateway(EgressConfig(allow_providers=["openai/gpt-4o"]))
        await _assert_contacts(gw, contact_counter, "chat", "openai/gpt-4o")

    async def test_case_and_whitespace_normalized(self, contact_counter):
        gw = _gateway(EgressConfig(allow_providers=["  OpenAI  "]))
        await _assert_contacts(gw, contact_counter, "chat", "openai/gpt-4o")

    async def test_disabled_switch_allows_even_with_allowlist(self, contact_counter):
        gw = _gateway(EgressConfig(enabled=False, allow_providers=["openai"]))
        await _assert_contacts(gw, contact_counter, "chat", "anthropic/claude-3")

    async def test_blank_entries_ignored(self, contact_counter):
        gw = _gateway(EgressConfig(allow_providers=["", "  "]))
        await _assert_contacts(gw, contact_counter, "chat", "x/y")


# ── 拒绝方向 ────────────────────────────────────────────────


class TestDenied:
    async def test_provider_prefix_denies_other_provider(self, contact_counter):
        gw = _gateway(EgressConfig(allow_providers=["openai"]))
        await _assert_denied(gw, contact_counter, "chat", "anthropic/claude-3")

    async def test_exact_entry_denies_other_model_of_same_provider(self, contact_counter):
        gw = _gateway(EgressConfig(allow_providers=["openai/gpt-4o"]))
        await _assert_denied(gw, contact_counter, "chat", "openai/gpt-4o-mini")

    async def test_unprefixed_model_denied_when_allowlist_nonempty(self, contact_counter):
        """无 provider 前缀的模型标识匹配不了任何条目 → fail-closed。"""
        gw = _gateway(EgressConfig(allow_providers=["openai"]))
        await _assert_denied(gw, contact_counter, "chat", "gpt-4o")

    @pytest.mark.parametrize("kind", ["chat", "chat_full", "chat_stream", "embedding"])
    async def test_all_entry_points_enforced(self, contact_counter, kind):
        gw = _gateway(EgressConfig(allow_providers=["openai"]))
        await _assert_denied(gw, contact_counter, kind, "anthropic/claude-3")

    async def test_error_does_not_echo_prompt(self, contact_counter):
        gw = _gateway(EgressConfig(allow_providers=["openai"]))
        with pytest.raises(LLMEgressDenied) as ei:
            await gw.chat(
                model="anthropic/claude-3",
                messages=[{"role": "user", "content": SECRET_PROMPT}],
            )
        assert SECRET_PROMPT not in str(ei.value)
        assert "anthropic" in str(ei.value)

    async def test_callback_receives_provider_and_model(self, contact_counter):
        seen: list[tuple[str, str]] = []
        gw = _gateway(
            EgressConfig(allow_providers=["openai"]),
            on_egress_denied=lambda p, m: seen.append((p, m)),
        )
        await _assert_denied(gw, contact_counter, "chat", "anthropic/claude-3")
        assert seen == [("anthropic", "anthropic/claude-3")]

    async def test_callback_failure_does_not_mask_denial(self, contact_counter):
        def boom(provider, model):
            raise RuntimeError("audit down")

        gw = _gateway(
            EgressConfig(allow_providers=["openai"]), on_egress_denied=boom,
        )
        await _assert_denied(gw, contact_counter, "chat", "anthropic/claude-3")

    async def test_metric_labelled_by_provider_only(self, contact_counter):
        gw = _gateway(EgressConfig(allow_providers=["openai"]))
        await _assert_denied(gw, contact_counter, "chat", "denytest-prov/anthropic")
        body = render_metrics().decode()
        assert 'trove_llm_egress_denied_total{provider="denytest-prov"}' in body


# ── 热更新 / fail-closed / mock 短路 ────────────────────────


class TestPolicyRead:
    async def test_callable_is_read_per_call(self, contact_counter):
        """管理端改活 config 的同一对象,下一次调用即生效(无重启)。"""
        cfg = EgressConfig(allow_providers=["openai"])
        gw = _gateway(lambda: cfg)
        await _assert_contacts(gw, contact_counter, "chat", "openai/gpt-4o")
        cfg.allow_providers = ["anthropic"]
        contact_counter.clear()  # 只断言热改之后那一次没有触网
        await _assert_denied(gw, contact_counter, "chat", "openai/gpt-4o")
        await _assert_contacts(gw, contact_counter, "chat", "anthropic/claude-3")

    async def test_policy_read_failure_fails_closed(self, contact_counter):
        def broken():
            raise RuntimeError("config gone")

        gw = _gateway(broken)
        await _assert_denied(gw, contact_counter, "chat", "openai/gpt-4o")

    async def test_mock_paths_short_circuit_before_denial(self, contact_counter):
        """离线测试/演练路径不受白名单影响(检查在 mock 之后)。"""
        deny = EgressConfig(allow_providers=["openai"])
        gw = _gateway(
            deny, mock_response="mocked", mock_stream_chunks=["a", "b"],
            mock_embedding=[[1.0, 0.0]],
        )
        assert await gw.chat(model="anthropic/claude-3", messages=MESSAGES) == "mocked"
        assert (await gw.chat_full(model="anthropic/claude-3", messages=MESSAGES))["content"] == "mocked"
        assert [c async for c in gw.chat_stream(model="anthropic/claude-3", messages=MESSAGES)] == ["a", "b"]
        assert await gw.embedding(model="anthropic/claude-3", inputs=["x"]) == [[1.0, 0.0]]
        assert contact_counter == []
