"""GET /v1/admin/overview 聚合端点测试(真实 API 往返;零网络、LLM mock)。

覆盖三条纪律:
1. 形状固定 —— 顶层与各块字段齐全,缺失 = null(不造假数字;0 与 null 是两条
   不同的信息)。
2. ``degraded[]`` 一等返回 —— 任一来源超时/抛错 → 该源一条 degraded 记录,
   HTTP 仍 200,受影响字段变 null/``count_exact: false``;只有内部存储探不通
   才是整页 503(与 /v1/health 同一判定)。
3. 未装配来源(skills/jobs/memory)归一为 ``available: false, count: 0`` ——
   是「没配」不是「坏了」。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from trove.api.routers import overview
from trove.services.jobs.service import JobsService
from trove.services.jobs.store import JobStore, Run
from trove.services.skills.service import SkillService

_TOP_LEVEL_KEYS = {
    "generated_at", "elapsed_ms", "window", "health", "usage", "todos",
    "datasources", "wizard", "recent_events", "degraded",
}
_TODO_KINDS = [
    "kb_lesson", "kb_example", "semantic_draft", "skill_draft",
    "memory_preference", "drift", "action_template", "action_proposal",
    "job_failed", "user_nogrant",
]


def _by_kind(body: dict) -> dict[str, dict]:
    return {item["kind"]: item for item in body["todos"]["items"]}


async def _seed_query_audit(
    auth, verdict: str, *, error: str = "", days_ago: float = 0.0,
) -> None:
    """写一条 ``query.execute`` 审计行(与 session._audit_query 同形状)。"""
    user = await auth.authenticate("admin", "adminpw")
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    await auth.store.append_audit(
        ts=ts, user_id=user["id"], username="admin", action="query.execute",
        method="POST", path="/v1/chat", status=200,
        details={
            "session_id": "s1", "run_id": "r1", "question": "平均贷款",
            "sql": "SELECT 1", "datasource": "test_db",
            "verdict": verdict, "row_count": 1, "execution_time_ms": 3,
            "error": error,
        },
    )


class TestOverviewShape:
    async def test_admin_roundtrip_full_shape(self, client, api_app):
        r = await client.get("/v1/admin/overview")
        assert r.status_code == 200, r.text
        body = r.json()

        # 顶层:字段齐全、顺序无关
        assert set(body) == _TOP_LEVEL_KEYS
        assert body["window"] == "24h"
        assert body["generated_at"]
        assert isinstance(body["elapsed_ms"], int) and body["elapsed_ms"] >= 0
        assert body["degraded"] == []

        # health:镜像 /v1/health 的判定
        health = body["health"]
        assert health["status"] in ("ok", "degraded")
        assert health["storage"]["ok"] is True
        assert health["llm"]["mock"] is True
        ping = health["datasources"]["test_db"]
        assert ping["ok"] is True
        # 没跑过启动自检 → not_probed,绝不报 verified: true
        assert ping["readonly"]["basis"] == "not_probed"
        assert ping["readonly"]["verified"] is None

        # usage:零审计行时 questions=0,但 success_rate 是 null —— 不造假数字
        usage = body["usage"]
        assert usage["available"] is True
        assert usage["questions"] == 0
        assert usage["ok"] == 0
        assert usage["success_rate"] is None
        assert usage["failures"] == 0
        assert usage["failures_by_class"] == []
        assert usage["failures_source"] == "audit_text"
        assert usage["failures_approximate"] is True
        assert usage["sample_capped"] is False

        assert body["recent_events"] == []

        # datasources:来自现有注册表 + KB 事实
        rows = body["datasources"]
        assert [row["name"] for row in rows] == ["test_db"]
        assert rows[0]["status"] == "connected"
        assert rows[0]["kb_initialized"] is False
        assert rows[0]["kb_items"] == {}
        assert rows[0]["refused"] == 0
        assert rows[0]["drift_open"] == 0
        assert rows[0]["drift_count_exact"] is True

        # todos:10 类固定顺序;未装配来源 → count 0 / available false
        todos = body["todos"]
        assert [i["kind"] for i in todos["items"]] == _TODO_KINDS
        by_kind = _by_kind(body)
        _, nogrant_total = await api_app.state.auth.list_users_page(
            status="nogrant", limit=3,
        )
        assert by_kind["user_nogrant"]["count"] == nogrant_total
        for kind in ("skill_draft", "memory_preference", "action_template",
                     "action_proposal", "job_failed"):
            assert by_kind[kind]["count"] == 0
            assert by_kind[kind]["available"] is False
            assert by_kind[kind]["count_exact"] is True
        assert todos["total"] == nogrant_total
        assert todos["count_exact"] is True

        # wizard:三步全读既有字段
        assert body["wizard"] == {
            "registered": 1,
            "kb_initialized": 0,
            "users_without_grant": nogrant_total,
        }

    async def test_kb_pending_assets_surface(self, client, api_app, api_kb):
        r = await client.get("/v1/admin/overview")
        assert r.status_code == 200, r.text
        body = r.json()

        row = body["datasources"][0]
        assert row["name"] == "test_db"
        assert row["kb_initialized"] is True
        assert row["kb_items"].get("lesson") == 1

        by_kind = _by_kind(body)
        # KB_SEED 的 lesson 是 confirmed: false → 待确认教训计数 1 + 样例
        assert by_kind["kb_lesson"]["count"] == 1
        assert by_kind["kb_lesson"]["samples"] == ["日期列误当文本比较"]
        # 示例没有 pending 标记 → 不虚报
        assert by_kind["kb_example"]["count"] == 0
        assert body["wizard"]["kb_initialized"] == 1


class TestOverviewUsage:
    async def test_usage_counts_and_failure_buckets(self, client, api_app):
        auth = api_app.state.auth
        await _seed_query_audit(auth, "OK")
        await _seed_query_audit(auth, "OK")
        await _seed_query_audit(
            auth, "RETRY",
            error="[ERR:SQL_SYNTAX] could not parse statement",
        )

        r = await client.get("/v1/admin/overview")
        assert r.status_code == 200, r.text
        usage = r.json()["usage"]
        assert usage["questions"] == 3
        assert usage["ok"] == 2
        assert usage["success_rate"] == pytest.approx(2 / 3, abs=1e-4)
        assert usage["failures"] == 1
        # 粗桶来自 [ERR:<id>] 打标 + 分类学域;整块标 approximate
        assert usage["failures_by_class"] == [
            {"class": "SQL_SYNTAX", "domain": "sql", "count": 1},
        ]

        events = r.json()["recent_events"]
        assert len(events) == 3
        assert events[0]["action"] == "query.execute"
        assert events[0]["username"] == "admin"
        assert events[0]["href"] == "/admin/audit?action=query.execute"

    async def test_window_filters_usage_and_events_only(self, client, api_app):
        auth = api_app.state.auth
        await _seed_query_audit(auth, "OK")
        await _seed_query_audit(auth, "OK", days_ago=10)

        r24 = await client.get("/v1/admin/overview", params={"window": "24h"})
        assert r24.json()["usage"]["questions"] == 1
        assert len(r24.json()["recent_events"]) == 1

        r30 = await client.get("/v1/admin/overview", params={"window": "30d"})
        assert r30.status_code == 200
        assert r30.json()["window"] == "30d"
        assert r30.json()["usage"]["questions"] == 2
        assert len(r30.json()["recent_events"]) == 2
        # health 与数据源面与窗口无关(两次一致)
        assert r30.json()["health"] == r24.json()["health"]
        assert r30.json()["datasources"] == r24.json()["datasources"]


class TestOverviewDegraded:
    async def test_one_source_failure_degrades_not_500(
        self, client, api_app, monkeypatch,
    ):
        async def boom(*_args, **_kwargs):
            raise RuntimeError("drift store down")

        monkeypatch.setattr(overview, "_drift_open", boom)
        r = await client.get("/v1/admin/overview")
        assert r.status_code == 200, r.text
        body = r.json()

        entry = next(d for d in body["degraded"] if d["source"] == "drift:test_db")
        assert entry["block"] == "datasources"
        assert entry["error"] == "RuntimeError"     # 只报类型名
        assert entry["at"]

        row = body["datasources"][0]
        assert row["drift_open"] is None            # 数不出来 = null,不是 0
        assert row["drift_count_exact"] is False

        drift_item = _by_kind(body)["drift"]
        assert drift_item["count"] == 0             # ≥ 0:降级时是下界
        assert drift_item["count_exact"] is False
        assert drift_item["available"] is False
        assert drift_item["note"] == "degraded"
        assert body["todos"]["count_exact"] is False

        # 单源失败不传染:健康横幅与 KB 事实照常
        assert body["health"]["storage"]["ok"] is True
        assert body["health"]["status"] == "ok"
        assert body["datasources"][0]["kb_initialized"] is False

    async def test_source_timeout_is_independent(
        self, client, api_app, monkeypatch,
    ):
        async def hang(*_args, **_kwargs):
            await asyncio.sleep(30)                 # 远高于 _SOURCE_TIMEOUT_S

        monkeypatch.setattr(overview, "_drift_open", hang)
        r = await client.get("/v1/admin/overview")
        assert r.status_code == 200, r.text
        body = r.json()
        entry = next(d for d in body["degraded"] if d["source"] == "drift:test_db")
        assert entry["error"] == "Timeout"
        assert body["usage"]["available"] is True    # 别的来源没被拖死

    async def test_audit_failure_degrades_usage_and_events(
        self, client, api_app, monkeypatch,
    ):
        async def boom():
            raise RuntimeError("audit store down")

        monkeypatch.setattr(api_app.state.auth, "count_audit", boom)
        r = await client.get("/v1/admin/overview")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["usage"] is None
        assert body["recent_events"] is None
        assert {(d["block"], d["source"], d["error"]) for d in body["degraded"]} == {
            ("usage", "audit", "RuntimeError"),
            ("recent_events", "audit", "RuntimeError"),
        }
        # 其余块不受影响
        assert body["health"]["status"] == "ok"
        assert body["datasources"][0]["name"] == "test_db"

    async def test_registry_failure_nulls_datasource_blocks(
        self, client, api_app, monkeypatch,
    ):
        def boom(_request):
            raise RuntimeError("registry down")

        monkeypatch.setattr(overview, "_datasource_names", boom)
        r = await client.get("/v1/admin/overview")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["datasources"] is None
        assert body["wizard"]["registered"] is None
        assert body["wizard"]["kb_initialized"] is None
        entry = next(d for d in body["degraded"] if d["source"] == "registry")
        assert entry["block"] == "datasources" and entry["error"] == "RuntimeError"
        # 逐源待办一并标降级(列不出源 = 数不出,不是 0)
        by_kind = _by_kind(body)
        for kind in ("kb_lesson", "kb_example", "semantic_draft", "drift"):
            assert by_kind[kind]["count"] is None
            assert by_kind[kind]["count_exact"] is False
            assert by_kind[kind]["available"] is False
        assert body["todos"]["count_exact"] is False
        assert body["health"]["status"] == "ok"      # 健康横幅不依赖列举

    async def test_storage_down_is_503_with_null_blocks(
        self, client, api_app, monkeypatch,
    ):
        class _BrokenBackend:
            async def execute(self, *_args, **_kwargs):
                raise RuntimeError("pg down")

            async def close(self) -> None:
                return None

        class _BrokenStore:
            _backend = _BrokenBackend()

        monkeypatch.setattr(
            api_app.state, "session_store", _BrokenStore(), raising=False,
        )
        r = await client.get("/v1/admin/overview")
        assert r.status_code == 503
        body = r.json()
        assert set(body) == _TOP_LEVEL_KEYS          # 503 也保持形状
        assert body["health"]["status"] == "unavailable"
        assert body["health"]["storage"] == {"ok": False, "error": "RuntimeError"}
        for block in ("usage", "todos", "datasources", "wizard", "recent_events"):
            assert body[block] is None
        assert body["degraded"][0]["source"] == "storage"


class TestOverviewJobsAndSkills:
    @pytest.fixture
    async def jobs_app(self, api_app, tmp_path):
        store = JobStore(tmp_path / "proj")
        jobs = JobsService(store)
        job = await jobs.create_job(
            "每月贷款总量", "30", "interval", datasource="test_db",
        )
        api_app.state.jobs = jobs
        yield api_app, jobs, job
        await store.dispose()

    async def test_failed_runs_surface_with_window(
        self, client, jobs_app, monkeypatch,
    ):
        api_app, jobs, job = jobs_app
        await jobs.store.start_run(Run(job_id=job.id, status="error"))
        await jobs.store.start_run(Run(
            job_id=job.id, status="error",
            started_at=(datetime.now(timezone.utc) - timedelta(days=10)).isoformat(),
        ))

        r = await client.get("/v1/admin/overview", params={"window": "24h"})
        assert r.status_code == 200, r.text
        item = _by_kind(r.json())["job_failed"]
        assert item["count"] == 1                    # 10 天前那次不进 24h 窗
        assert item["available"] is True
        assert item["samples"] == [job.name]
        assert item["href"] == "/admin/jobs?status=error"

        r30 = await client.get("/v1/admin/overview", params={"window": "30d"})
        assert _by_kind(r30.json())["job_failed"]["count"] == 2

    async def test_pending_skill_draft_surfaces(self, client, api_app, tmp_path):
        api_app.state.skills = SkillService(root=tmp_path / "proj" / ".trove" / "skills")
        r = await client.post("/v1/admin/skills/draft", json={
            "name": "recon-caliber",
            "description": "对账口径",
            "triggers": {"node": "query_sketch"},
            "tier": "available",
            "body": "1. diff 行级\n2. 对总额\n",
        })
        assert r.status_code == 201, r.text

        r = await client.get("/v1/admin/overview")
        item = _by_kind(r.json())["skill_draft"]
        assert item["count"] == 1
        assert item["samples"] == ["recon-caliber"]
        assert item["available"] is True
        assert item["href"] == "/admin/skills"


class TestOverviewAuthAndWindow:
    async def test_non_admin_403_anon_401(self, user_client, anon_client):
        assert (await user_client.get("/v1/admin/overview")).status_code == 403
        r = await anon_client.get("/v1/admin/overview")
        assert r.status_code in (401, 403)

    async def test_invalid_window_400(self, client):
        for bad in ("abc", "24", "0h", "24x", "200d"):
            r = await client.get("/v1/admin/overview", params={"window": bad})
            assert r.status_code == 400, f"{bad} -> {r.status_code}"

    async def test_window_echoed(self, client):
        r = await client.get("/v1/admin/overview", params={"window": "7d"})
        assert r.status_code == 200
        assert r.json()["window"] == "7d"
