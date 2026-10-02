"""``GET /v1/admin/usage/overview`` 成本与性能运营台端点测试(真实往返;零网络)。

覆盖设计稿 §4.2 的口径纪律 + 2026-10-02 裁决③/④:

1. 三个值各有各的意思:``null`` = 没测到(或没装配),``0`` = 测到且为零,
   ``[]`` = 空结果 —— 未装配的会话存储不许显示成 0 条消息。
2. 成本采样到顶(``sample_capped``)时,一切**求和类**字段回 null(截断过的
   和在数学上就是错的),**计数类**字段仍精确(独立 COUNT 查询)。
3. token 缓存字段 take-one-not-sum(``cached_tokens`` 与
   ``cache_read_input_tokens`` 是同一个数的两种拼法,不叠加)。
4. 延迟只在 serve 模式(有 auth)有数据;没有审计面 = 未测到,不是降级。
5. 窗口过滤同时作用于审计与消息;非法窗口 400;存储探不通 → 503(整页唯一
   失败形状,其余块 null)。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trove.api.routers import usage
from trove.core.types import Message, Session
from trove.storage.session_store import SessionStore

_TOP_LEVEL_KEYS = {
    "available", "window", "cost", "budget", "cache", "latency",
    "not_measured", "degraded", "generated_at",
}
_COST_KEYS = {
    "source", "sampled", "sample_capped", "sample_max", "tokens",
    "cache_tokens", "unmeasured", "per_question", "by_model",
}
_LATENCY_KEYS = {
    "basis", "n", "sample_capped", "p50_ms", "p95_ms", "series", "end_to_end",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _seed_latency(auth, rows: list[tuple[int | None, float]]) -> None:
    """写 ``query.execute`` 审计行:(execution_time_ms, days_ago)。"""
    user = await auth.authenticate("admin", "adminpw")
    for ms, days_ago in rows:
        details: dict = {"verdict": "OK", "row_count": 1}
        if ms is not None:
            details["execution_time_ms"] = ms
        await auth.store.append_audit(
            ts=(_now() - timedelta(days=days_ago)).isoformat(),
            user_id=user["id"], username="admin", action="query.execute",
            method="POST", path="/v1/chat", status=200, details=details,
        )


async def _seed_messages(
    store: SessionStore, messages: list[Message], *, session_id: str = "s1",
) -> None:
    await store.save_session(Session(
        project_name="proj", session_id=session_id, messages=messages,
    ))


def _assistant(metadata: dict, *, minutes_ago: int = 0) -> Message:
    return Message(
        role="assistant", content="answer", metadata=metadata,
        timestamp=_now() - timedelta(minutes=minutes_ago),
    )


@pytest.fixture
async def sess_store(api_app, tmp_path):
    """挂在 app.state 上的真实会话存储(未接时端点须报"没装配")。"""
    store = SessionStore(home_dir=tmp_path / "sess")
    api_app.state.session_store = store
    yield store
    await store.dispose()          # aiosqlite 线程不 dispose 会挂住进程退出


class TestUsageShape:
    async def test_no_data_is_honest_null_not_zero(self, client, api_app):
        r = await client.get("/v1/admin/usage/overview")
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body) == _TOP_LEVEL_KEYS
        assert body["window"]["kind"] == "7d" and body["window"]["basis"] == "audit.ts"
        since = datetime.fromisoformat(body["window"]["since"])
        until = datetime.fromisoformat(body["window"]["until"])
        assert (until - since) == timedelta(days=7)
        assert body["not_measured"] == [
            "cost.by_model", "cache.answer", "latency.end_to_end", "cost.spend",
        ]
        assert set(body["cost"]) == _COST_KEYS
        # 没装会话存储 → null(不是 0 条消息)
        assert body["cost"]["sampled"] is None
        assert body["cost"]["unmeasured"] == {
            "assistant_messages": None, "without_usage": None, "ratio": None,
        }
        # 有 auth(测试 app 装了)→ 审计面在,只是窗口内没有行:n=0 是"测到且为零"
        lat = body["latency"]
        assert set(lat) == _LATENCY_KEYS
        assert lat["n"] == 0 and lat["p50_ms"] is None and lat["series"] == []
        assert lat["end_to_end"] is None
        assert lat["basis"] == "audit.details.execution_time_ms"
        # 预算:进程快照(形状在,行数取决于进程里跑过什么)
        assert body["budget"]["source"] == "prometheus_snapshot"
        assert body["budget"]["lifetime"] == "process"
        for row in body["budget"]["decisions"]:
            assert set(row) == {"datasource", "source", "verdict", "count"}
        # 缓存:scorecard 不存在 → prompt null + degraded(腿级,不拖垮全页)
        assert set(body["cache"]) == {"connector", "answer", "prompt"}
        assert body["cache"]["answer"] is None and body["cache"]["prompt"] is None
        entry = next(d for d in body["degraded"] if d["block"] == "cache")
        assert entry["error"] == "FileNotFoundError"
        assert not [d for d in body["degraded"] if d["block"] in ("cost", "latency")]

    async def test_unwired_sources_report_not_available_not_degraded(
        self, client, api_app, monkeypatch,
    ):
        monkeypatch.setattr(usage, "_audit_backend", lambda _request: None)
        r = await client.get("/v1/admin/usage/overview")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["available"] is False        # 没装配 ≠ 坏了
        assert body["latency"] is None
        assert body["cost"]["sampled"] is None
        assert not [d for d in body["degraded"] if d["block"] in ("cost", "latency", "storage")]


class TestUsageWindow:
    async def test_window_filters_audit_rows(self, client, api_app, auth_service):
        # 29 天前(不是 30):边界上差几毫秒就会落在 30d 窗口之外,测试不测边界。
        await _seed_latency(auth_service, [(100, 0), (200, 0), (300, 0), (400, 0), (500, 29)])
        r = await client.get("/v1/admin/usage/overview", params={"window": "7d"})
        assert r.status_code == 200, r.text
        lat = r.json()["latency"]
        assert lat["n"] == 4
        assert lat["p50_ms"] == 200 and lat["p95_ms"] == 400
        assert len(lat["series"]) == 1
        assert lat["series"][0]["n"] == 4 and lat["series"][0]["p50_ms"] == 200
        assert lat["sample_capped"] is False

        r = await client.get("/v1/admin/usage/overview", params={"window": "30d"})
        lat = r.json()["latency"]
        assert lat["n"] == 5                     # 30 天前那条被 7d 滤掉、被 30d 收进
        assert lat["p50_ms"] == 300 and lat["p95_ms"] == 500

    async def test_rows_without_execution_time_are_not_latency_samples(
        self, client, api_app, auth_service,
    ):
        await _seed_latency(auth_service, [(120, 0), (None, 0)])
        lat = (await client.get("/v1/admin/usage/overview")).json()["latency"]
        assert lat["n"] == 1                     # 缺字段的行不进 n(也不伪造 0ms)
        assert lat["p50_ms"] == 120

    async def test_invalid_window_400(self, client):
        r = await client.get("/v1/admin/usage/overview", params={"window": "banana"})
        assert r.status_code == 400

    async def test_window_filters_messages(self, client, api_app, sess_store):
        await _seed_messages(sess_store, [
            _assistant({"token_usage": {"prompt": 10, "completion": 5, "total": 15}}),
            _assistant(
                {"token_usage": {"prompt": 1, "completion": 1, "total": 2}},
                minutes_ago=60 * 24 * 40,        # 40 天前:7d 与 30d 都够不着
            ),
        ])
        body7 = (await client.get("/v1/admin/usage/overview", params={"window": "7d"})).json()
        assert body7["cost"]["unmeasured"]["assistant_messages"] == 1
        assert body7["cost"]["tokens"]["total"] == 15
        body30 = (await client.get("/v1/admin/usage/overview", params={"window": "30d"})).json()
        assert body30["cost"]["unmeasured"]["assistant_messages"] == 1
        body90 = (await client.get("/v1/admin/usage/overview", params={"window": "90d"})).json()
        assert body90["cost"]["unmeasured"]["assistant_messages"] == 2
        assert body90["cost"]["tokens"]["total"] == 17


class TestUsageCost:
    async def test_tokens_cache_and_unmeasured_ratio(self, client, api_app, sess_store):
        await _seed_messages(sess_store, [
            _assistant({
                "token_usage": {
                    "prompt": 1000, "completion": 200, "total": 1200,
                    # take-one-not-sum:prefer cached_tokens(999 不叠加)
                    "cached_tokens": 100, "cache_read_input_tokens": 999,
                },
            }),
            _assistant({"token_usage": {
                "prompt": 300, "completion": 100, "total": 400,
                "cache_read_input_tokens": 50,
            }}),
            _assistant({}),                       # 无 token_usage:计未测量
            Message(role="user", content="q", metadata={
                "token_usage": {"prompt": 7, "completion": 7, "total": 14},
            }),                                   # 非 assistant:不进任何计数
        ])
        r = await client.get("/v1/admin/usage/overview")
        cost = r.json()["cost"]
        assert cost["source"] == "message_metadata"
        assert cost["sampled"] == 2 and cost["sample_capped"] is False
        assert cost["tokens"] == {"prompt": 1300, "completion": 300, "total": 1600}
        assert cost["cache_tokens"] == 150        # 100 + 50,不是 100+999+50
        assert cost["unmeasured"] == {
            "assistant_messages": 3, "without_usage": 1, "ratio": 0.3333,
        }
        assert cost["per_question"] == {"mean_total": 800, "median_total": 800}
        assert cost["by_model"] is None

    async def test_sample_cap_nulls_sums_keeps_exact_counts(
        self, client, api_app, sess_store, monkeypatch,
    ):
        monkeypatch.setattr(usage, "_SAMPLE_MAX", 2)
        await _seed_messages(sess_store, [
            _assistant({"token_usage": {"prompt": 1, "completion": 1, "total": 2}}),
            _assistant({"token_usage": {"prompt": 1, "completion": 1, "total": 2}}),
            _assistant({"token_usage": {"prompt": 1, "completion": 1, "total": 2}}),
        ])
        cost = (await client.get("/v1/admin/usage/overview")).json()["cost"]
        assert cost["sampled"] == 2 and cost["sample_capped"] is True
        assert cost["sample_max"] == 2
        # 求和类字段:截断过的和是错的 → null(不是 4)
        assert cost["tokens"] is None and cost["cache_tokens"] is None
        assert cost["per_question"] is None
        # 计数类字段:独立 COUNT,仍然精确
        assert cost["unmeasured"] == {
            "assistant_messages": 3, "without_usage": 0, "ratio": 0.0,
        }

    async def test_no_usage_at_all_is_null_not_zero(self, client, api_app, sess_store):
        await _seed_messages(sess_store, [_assistant({}), _assistant({})])
        cost = (await client.get("/v1/admin/usage/overview")).json()["cost"]
        assert cost["sampled"] == 0
        assert cost["tokens"] is None             # 一条都没测到 ≠ 0 token
        assert cost["unmeasured"]["assistant_messages"] == 2
        assert cost["unmeasured"]["ratio"] == 1.0


class TestUsageFailure:
    async def test_storage_down_is_503_with_null_blocks(self, client, api_app, monkeypatch):
        class _BrokenBackend:
            async def execute(self, *_args, **_kwargs):
                raise RuntimeError("pg down")

            async def close(self) -> None:
                return None

        class _BrokenStore:
            _backend = _BrokenBackend()

        monkeypatch.setattr(api_app.state, "session_store", _BrokenStore(), raising=False)
        r = await client.get("/v1/admin/usage/overview")
        assert r.status_code == 503
        body = r.json()
        assert set(body) == _TOP_LEVEL_KEYS       # 503 也保持形状
        assert body["available"] is False
        for block in ("cost", "budget", "cache", "latency"):
            assert body[block] is None
        assert body["degraded"][0]["source"] == "storage"

    async def test_cost_leg_failure_degrades_alone(self, client, api_app, sess_store, monkeypatch):
        async def boom(_request, _cutoff):
            raise RuntimeError("messages table down")

        monkeypatch.setattr(usage, "_cost_block", boom)
        r = await client.get("/v1/admin/usage/overview")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["cost"] is None
        entry = next(d for d in body["degraded"] if d["block"] == "cost")
        assert entry["source"] == "messages" and entry["error"] == "RuntimeError"
        assert body["latency"] is not None        # 别的腿没被拖死


class TestUsageAuth:
    async def test_non_admin_403_anon_401(self, user_client, anon_client):
        r = await user_client.get("/v1/admin/usage/overview")
        assert r.status_code == 403
        r = await anon_client.get("/v1/admin/usage/overview")
        assert r.status_code == 401
