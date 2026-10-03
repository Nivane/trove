"""GET /v1/admin/todos —— 八类审批待办的条目级聚合(设计稿 P5 §4.1①)。

覆盖:
1. 八类计数 + 条目形状(字段名逐字;缺失 = null,形状固定);
2. kind / ds / q / sort / limit / offset 六个查询参数(limit 钳制 ≤200,不 422);
3. **0 与 null 是两条信息** —— 某类取不到 → ``counts[kind] = null`` +
   ``degraded[]`` 条目(绝不落成 0),``total`` 随之 null;
4. **同源同值(R2)** —— 与 ``/v1/admin/overview`` 的 ``todos[]`` 逐类计数相等
   (同一份 ``collect_todo_sources``,两个投影);
5. 权限 —— ``require_admin`` 自持门禁(非 admin 403;admin 账号的受限
   query-only token 亦 403)。
"""

from __future__ import annotations

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from trove.api.routers import governance, overview
from trove.services.skills.service import SkillService

EIGHT = (
    "kb_lesson", "kb_example", "semantic_draft",
    "skill_draft", "memory_preference", "drift",
    "action_template", "action_proposal",
)
ITEM_KEYS = {
    "kind", "id", "ds", "title", "summary", "severity", "confidence",
    "created_at", "href", "actionable", "diff", "source",
}
ACTIONABLE_KEYS = {"confirm", "reject", "batch", "edit_url"}


def _root(api_app):
    return api_app.state.kb.kb_dir.parent.parent


def _by_kind(body: dict) -> dict[str, dict]:
    return {i["kind"]: i for i in body["items"]}


async def _write_lessons(api_app, lessons: list[dict]) -> None:
    kb = api_app.state.kb
    path = kb.kb_dir / "test_db" / "lessons.yml"
    path.write_text(
        yaml.safe_dump({"lessons": lessons}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    await kb.ensure_synced("test_db")


async def _install_skills(api_app) -> SkillService:
    svc = SkillService(root=_root(api_app) / ".trove" / "skills")
    api_app.state.skills = svc
    return svc


async def _seed_skill_draft(client, name: str = "recon-caliber") -> None:
    r = await client.post("/v1/admin/skills/draft", json={
        "name": name,
        "description": "对账口径",
        "triggers": {"node": "query_sketch"},
        "tier": "available",
        "body": "1. diff 行级\n2. 对总额\n",
    })
    assert r.status_code == 201, r.text


async def _seed_semantic_draft(client) -> None:
    r = await client.post("/v1/admin/semantic/test_db/drafts", json={
        "kind": "metric", "action": "upsert", "name": "最高成绩",
        "payload": {"expression": "MAX(students.grade)"},
        "note": "口径补充",
    })
    assert r.status_code == 201, r.text


@pytest.fixture
async def memory_app(api_app, tmp_path):
    """装一个 enabled 的 MemoryService + 一条 pending 偏好(生产装配见 main.py)。"""
    from trove.services.memory.models import MemoryConfig, MemoryScope
    from trove.services.memory.service import MemoryService

    mem = MemoryService(
        tmp_path / "mem", MemoryConfig(enabled=True), kb=api_app.state.kb,
    )
    await mem.preferences.add(
        MemoryScope(user_id="1", datasource="test_db"),
        "金额统一用万元",
        evidence="会话里纠正过一次",
        confidence=0.6,
    )
    api_app.state.memory = mem
    yield mem
    # aiosqlite worker 线程常驻非 daemon,不 dispose 会挂住 pytest 退出
    # (与 auth_service fixture 同一理由)。
    await mem.preferences._backend.dispose()


async def _seed_drift(api_app, items) -> None:
    from trove.services.drift import DriftReport, DriftStore

    store = DriftStore(_root(api_app))
    try:
        await store.record(DriftReport(
            datasource="test_db", status="ok",
            generated_at="2026-01-01T00:00:00+00:00", items=items,
        ))
    finally:
        await store.dispose()


async def _seed_one_drift(api_app, subject="courses.ghost", severity="warning") -> None:
    from trove.services.drift import DriftItem

    await _seed_drift(api_app, [DriftItem(
        level="L2", kind="missing_field", subject=subject, severity=severity,
    )])


class TestTodosShape:
    async def test_six_kinds_counts_and_item_shape(
        self, client, api_app, api_kb, memory_app,
    ):
        """五类各来一条(example 缺席)= 空态不是「没数到」,是「确实是 0」。"""
        await _install_skills(api_app)
        await _seed_semantic_draft(client)
        await _seed_skill_draft(client)
        await _seed_one_drift(api_app)

        r = await client.get("/v1/admin/todos")
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body) == {"items", "total", "counts", "generated_at", "degraded"}
        assert body["generated_at"]
        assert body["degraded"] == []
        # counts 恒含八类(筛选片要靠它),无该类条目 = 0(exact),不是 null
        assert set(body["counts"]) == set(EIGHT)
        assert body["counts"] == {
            "kb_lesson": 1, "kb_example": 0, "semantic_draft": 1,
            "skill_draft": 1, "memory_preference": 1, "drift": 1,
            "action_template": 0, "action_proposal": 0,
        }
        assert body["total"] == 5
        assert len(body["items"]) == 5
        for item in body["items"]:
            assert set(item) == ITEM_KEYS
            assert set(item["actionable"]) == ACTIONABLE_KEYS

        by = _by_kind(body)

        # kb_lesson:KB_SEED 的 pending 教训;diff 是新增条目的合成 diff
        lesson = by["kb_lesson"]
        assert lesson["id"] == "日期列误当文本比较"
        assert lesson["ds"] == "test_db"
        assert lesson["href"] == "/admin/kb?tab=lessons"
        assert lesson["actionable"] == {
            "confirm": True, "reject": True, "batch": True,
            "edit_url": "/admin/kb?tab=lessons",
        }
        assert lesson["diff"]["before"] is None
        assert lesson["diff"]["fields"][0]["f"] == "note"
        assert lesson["diff"]["fields"][0]["changed"] is True

        # semantic_draft:diff 是服务端算的(契约字段全在),note 进 summary
        sem = by["semantic_draft"]
        assert sem["title"] == "最高成绩"
        assert sem["summary"] == "口径补充"
        assert sem["id"]
        assert set(sem["diff"]) == {
            "kind", "name", "action", "before", "after", "fields", "error",
        }
        assert sem["diff"]["error"] is None
        assert sem["diff"]["after"]["expression"] == "MAX(students.grade)"
        assert sem["actionable"]["edit_url"] == "/admin/semantic?pending=1"

        # skill_draft:ds 为 null(全局资产,不属于任何数据源)
        skill = by["skill_draft"]
        assert skill["title"] == "recon-caliber"
        assert skill["ds"] is None
        assert skill["href"] == "/admin/skills"

        # memory_preference:href 落治理中心收件箱(P5 起不再是 null)
        mem = by["memory_preference"]
        assert mem["title"] == "金额统一用万元"
        assert mem["summary"] == "会话里纠正过一次"
        assert mem["confidence"] == 0.6
        assert mem["href"] == "/admin/governance?tab=inbox&kind=memory_preference"
        assert mem["ds"] == "test_db"
        assert mem["actionable"]["edit_url"] is None   # 就地确认/驳回,无编辑器

        # drift:只给入口,不在收件箱里直接裁定
        drift = by["drift"]
        assert drift["title"] == "courses.ghost"
        assert drift["severity"] == "warning"
        assert drift["summary"] == "L2 · missing_field"
        assert drift["href"] == "/admin/governance?tab=drift"
        assert drift["actionable"] == {
            "confirm": False, "reject": False, "batch": False, "edit_url": None,
        }

    async def test_kind_filter_narrows_items_not_counts(self, client, api_app, api_kb):
        await _seed_semantic_draft(client)

        r = await client.get(
            "/v1/admin/todos", params={"kind": "semantic_draft,kb_lesson"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert {i["kind"] for i in body["items"]} == {"semantic_draft", "kb_lesson"}
        assert body["total"] == 2
        # counts 忽略 kind 筛选:八类全在,数字不变
        assert set(body["counts"]) == set(EIGHT)
        assert body["counts"]["kb_lesson"] == 1
        assert body["counts"]["kb_example"] == 0

    async def test_ds_and_q_filters(self, client, api_app, api_kb):
        await _install_skills(api_app)
        await _seed_skill_draft(client)

        # q 是 title/summary/ds 的大小写不敏感子串
        body = (await client.get(
            "/v1/admin/todos", params={"q": "日期"},
        )).json()
        assert [i["kind"] for i in body["items"]] == ["kb_lesson"]
        assert body["total"] == 1

        # ds=test_db:属于该源的四类照常;无源全局类(skill)被筛掉 → 0
        body = (await client.get(
            "/v1/admin/todos", params={"ds": "test_db"},
        )).json()
        assert body["counts"]["kb_lesson"] == 1
        assert body["counts"]["skill_draft"] == 0
        assert body["degraded"] == []


class TestTodosPaging:
    async def test_limit_clamped_and_offset_paging(self, client, api_app, api_kb):
        await _write_lessons(api_app, [
            {"pattern": f"教训 {i:03d}", "note": "n", "confirmed": False}
            for i in range(210)
        ])
        # 210 > 200:limit 越界**钳制**到 200(与 semantic history 同手法,不 422)
        body = (await client.get(
            "/v1/admin/todos", params={"kind": "kb_lesson", "limit": 500},
        )).json()
        assert len(body["items"]) == 200
        assert body["total"] == 210
        assert body["counts"]["kb_lesson"] == 210

        # limit 0 → 钳到 1;非法值 → 回落默认 50
        body = (await client.get(
            "/v1/admin/todos", params={"kind": "kb_lesson", "limit": 0},
        )).json()
        assert len(body["items"]) == 1
        body = (await client.get(
            "/v1/admin/todos", params={"kind": "kb_lesson", "limit": "abc"},
        )).json()
        assert len(body["items"]) == 50

        # offset 分页确定性:两页拼接 == 全量切片(同值条目不抖动)
        full = (await client.get(
            "/v1/admin/todos", params={"kind": "kb_lesson", "limit": 200},
        )).json()
        page = (await client.get(
            "/v1/admin/todos",
            params={"kind": "kb_lesson", "limit": 60, "offset": 20},
        )).json()
        assert [i["id"] for i in page["items"]] == [
            i["id"] for i in full["items"][20:80]
        ]

    async def test_sort_missing_values_last(self, client, api_app, api_kb, memory_app):
        await _seed_one_drift(api_app, subject="students.grade", severity="warning")

        # severity:有严重度的在前,无严重度的(lesson/skill)恒在最后
        body = (await client.get(
            "/v1/admin/todos", params={"sort": "severity"},
        )).json()
        assert body["items"][0]["kind"] == "drift"
        sev = [i["severity"] for i in body["items"]]
        first_missing = next(idx for idx, s in enumerate(sev) if s is None)
        assert all(s is None for s in sev[first_missing:])

        # confidence:有值的在前且降序,缺失恒在最后
        body = (await client.get(
            "/v1/admin/todos", params={"sort": "confidence"},
        )).json()
        assert body["items"][0]["kind"] == "memory_preference"
        conf = [i["confidence"] for i in body["items"]]
        first_missing = next(idx for idx, c in enumerate(conf) if c is None)
        assert all(c is None for c in conf[first_missing:])
        assert conf[:first_missing] == sorted(conf[:first_missing], reverse=True)

        # created_at 缺失恒在最后(没时间的条目不冒充"最新")
        body = (await client.get(
            "/v1/admin/todos", params={"sort": "newest"},
        )).json()
        created = [i["created_at"] for i in body["items"]]
        first_missing = next(
            (idx for idx, c in enumerate(created) if not c), len(created),
        )
        assert all(not c for c in created[first_missing:])
        assert first_missing > 0

    async def test_unknown_kind_or_sort_400(self, client, api_kb):
        r = await client.get("/v1/admin/todos", params={"kind": "nope"})
        assert r.status_code == 400
        assert "kb_lesson" in r.json()["detail"]      # 报出已知值
        r = await client.get("/v1/admin/todos", params={"sort": "huh"})
        assert r.status_code == 400


class TestTodosDegradedAndEmpty:
    async def test_empty_state_is_zero_not_null(self, client, api_app):
        """全空是真空态:items [] / total 0 / 八类全 0(exact),无 degraded。"""
        r = await client.get("/v1/admin/todos")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["items"] == []
        assert body["total"] == 0
        assert set(body["counts"]) == set(EIGHT)
        assert all(v == 0 for v in body["counts"].values())
        assert body["degraded"] == []

    async def test_one_kind_failure_counts_null_not_zero(
        self, client, api_app, api_kb, monkeypatch,
    ):
        """单类取不到 → counts[kind] null + degraded;其余类不传染。"""
        async def boom(*_args, **_kwargs):
            raise RuntimeError("drift store down")

        monkeypatch.setattr(overview, "_drift_open", boom)
        r = await client.get("/v1/admin/todos")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["counts"]["drift"] is None          # 数不出来 ≠ 0
        assert body["total"] is None                    # 有一类拿不到 → 合计不冒充
        assert all(i["kind"] != "drift" for i in body["items"])
        entry = next(d for d in body["degraded"] if d["kind"] == "drift")
        assert entry["ds"] == "test_db"
        assert entry["error"] == "RuntimeError"         # 只报类型名
        assert entry["at"]
        # 其它类照常
        assert body["counts"]["kb_lesson"] == 1
        assert body["counts"]["kb_example"] == 0

    async def test_capped_drift_count_is_labeled_lower_bound(
        self, client, api_app, api_kb, monkeypatch,
    ):
        """漂移计数到扫描帽 = 下界:degraded 里必有一条 kind=drift 的 capped 标注。

        契约没有 count_exact 字段,下界信号只能走 degraded[](与 overview 的
        note: "capped" 同一件事)—— 否则「≥2000」会被读成精确值。
        """
        async def capped(_request, _name, *, limit=0):
            rows = [{
                "id": i, "ds": "test_db", "level": "schema", "kind": "missing",
                "subject": f"col_{i}", "severity": "warning", "status": "open",
                "source": "drift", "first_seen_at": None, "last_seen_at": None,
                "seen_count": 1,
            } for i in range(limit)]
            # 真实饱和形状:条目数 == 扫描帽,exact=False(计数即下界)
            return {"count": len(rows), "samples": [], "exact": False, "entries": rows}

        monkeypatch.setattr(governance, "_TODO_SCAN_CAP", 3)
        monkeypatch.setattr(overview, "_drift_open", capped)
        body = (await client.get("/v1/admin/todos")).json()
        assert body["counts"]["drift"] == 3               # 数得出来,只是下界
        assert body["total"] == 4                         # 1 条 KB lesson + 3 漂移
        entry = next(d for d in body["degraded"] if d["kind"] == "drift")
        assert (entry["ds"], entry["error"]) == ("test_db", "capped")
        assert entry["at"]
        # 只是下界标注,不是整类取不到:别的类不受影响
        assert body["counts"]["kb_lesson"] == 1
        assert body["counts"]["kb_example"] == 0

    async def test_per_source_leg_failure_nulls_only_that_kind(
        self, client, api_app, api_kb, monkeypatch,
    ):
        async def boom(*_args, **_kwargs):
            raise RuntimeError("kb mirror down")

        monkeypatch.setattr(api_app.state.kb, "list_lessons", boom)
        body = (await client.get("/v1/admin/todos")).json()
        assert body["counts"]["kb_lesson"] is None
        assert body["counts"]["kb_example"] == 0
        assert body["total"] is None
        entry = next(d for d in body["degraded"] if d["kind"] == "kb_lesson")
        assert (entry["ds"], entry["error"]) == ("test_db", "RuntimeError")

    async def test_registry_failure_nulls_all_enum_kinds(
        self, client, api_app, monkeypatch,
    ):
        """源都列不出来 → 逐源四类各一条 degraded(ds null),绝不冒充 0。"""
        def boom(_request):
            raise RuntimeError("registry down")

        monkeypatch.setattr(overview, "_datasource_names", boom)
        body = (await client.get("/v1/admin/todos")).json()
        for kind in ("kb_lesson", "kb_example", "semantic_draft", "drift"):
            assert body["counts"][kind] is None, kind
        assert body["total"] is None
        assert body["items"] == []
        degraded = {(d["kind"], d["ds"]) for d in body["degraded"]}
        for kind in ("kb_lesson", "kb_example", "semantic_draft", "drift"):
            assert (kind, None) in degraded
        # 全局四类不依赖逐源列举:未装配 → 0(数得出来)
        assert body["counts"]["skill_draft"] == 0
        assert body["counts"]["memory_preference"] == 0
        assert body["counts"]["action_template"] == 0
        assert body["counts"]["action_proposal"] == 0


class TestTodosSameSourceAsOverview:
    async def test_r2_counts_equal_overview_todos(
        self, client, api_app, api_kb, memory_app,
    ):
        """验收 R2:收件箱计数与概览页 todos[] 同源同值(八类逐类相等)。"""
        await _install_skills(api_app)
        await _seed_semantic_draft(client)
        await _seed_skill_draft(client)
        await _seed_one_drift(api_app)

        todos = (await client.get("/v1/admin/todos")).json()
        ov = (await client.get("/v1/admin/overview")).json()
        by_kind = {i["kind"]: i for i in ov["todos"]["items"]}
        for kind in EIGHT:
            assert todos["counts"][kind] == by_kind[kind]["count"], kind
        assert todos["counts"]["kb_lesson"] == 1       # 夹具非空,比较有意义

    async def test_r2_degradation_is_also_shared(
        self, client, api_app, api_kb, monkeypatch,
    ):
        """同一处降级在两边同现:一边 null/下界,一边也 null/下界。"""
        async def boom(*_args, **_kwargs):
            raise RuntimeError("drift store down")

        monkeypatch.setattr(overview, "_drift_open", boom)
        todos = (await client.get("/v1/admin/todos")).json()
        ov = (await client.get("/v1/admin/overview")).json()
        drift_item = next(
            i for i in ov["todos"]["items"] if i["kind"] == "drift"
        )
        assert todos["counts"]["drift"] is None
        assert drift_item["count_exact"] is False
        assert drift_item["available"] is False


class TestTodosAuth:
    async def test_non_admin_403_anon_401(self, user_client, anon_client, api_app):
        assert (await user_client.get("/v1/admin/todos")).status_code == 403
        r = await anon_client.get("/v1/admin/todos")
        assert r.status_code in (401, 403)

    async def test_admin_scoped_token_needs_admin_scope(
        self, api_app, auth_service,
    ):
        """管理账号签发的 query-only token 不得借角色拿管理权(deps.py:120-134)。"""
        admin = await auth_service.authenticate("admin", "adminpw")
        raw, _ = await auth_service.create_token(
            admin["id"], label="query-only", scopes=["query"],
        )
        transport = ASGITransport(app=api_app)
        headers = {"Authorization": f"Bearer {raw}"}
        async with AsyncClient(
            transport=transport, base_url="http://test", headers=headers,
        ) as c:
            r = await c.get("/v1/admin/todos")
            assert r.status_code == 403
            assert "scope" in r.json()["detail"]


class TestAutoCandidateInbox:
    """软 MISS 自动候选的收件箱面(补丁 3「候选收件箱」的交付验收)。"""

    async def test_auto_candidate_lands_with_source_marker(
        self, client, api_app, api_kb,
    ):
        """捕获口落的 pending 草稿出现在 semantic_draft 源,summary 即 note。"""
        from trove.services.semantic_layer.candidates import capture_candidates

        created = await capture_candidates(
            api_app.state.kb, "test_db", "最高成绩是多少?",
            [{"reason": "no_metric_match", "component": "MAX(students.grade)"}])
        assert len(created) == 1

        r = await client.get("/v1/admin/todos", params={"kind": "semantic_draft"})
        assert r.status_code == 200, r.text
        items = [i for i in r.json()["items"] if i["kind"] == "semantic_draft"]
        assert len(items) == 1
        item = items[0]
        assert item["title"] == "max_grade"
        # 来源标在 note 上 —— 收件箱 summary 逐字即 note,管理员一眼分辨
        # 自动候选与人工/refuse 草稿。
        assert item["summary"] == "auto:no_metric_match:最高成绩是多少?"
        assert item["source"] == "metric"
        assert item["actionable"]["edit_url"] == "/admin/semantic?pending=1"
