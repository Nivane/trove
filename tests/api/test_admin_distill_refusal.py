"""A5:蒸馏管理端(202/轮询/409/失败不吞)+ 拒绝频率报表(只读投影)。

零网络/零 LLM:蒸馏的确定性部分跑真 KB 目录上的真实管线;成功记录不是
教训材料 → 生命周期测试根本不触发 LLM(触发的那条路径用 fail 断言「空
输入失败」,也不调模型)。
"""

from __future__ import annotations

import asyncio

from trove.services.auth.store import now_iso


async def _register(client, name: str, tmp_path) -> None:
    resp = await client.post("/v1/admin/datasources",
                             json={"name": name, "url": "sqlite://:memory:"})
    assert resp.status_code in (200, 201), (resp.status_code, resp.text)


async def _wait_distill(client, name: str, timeout: float = 10.0) -> dict:
    """轮询蒸馏状态直到 done/error(返回最终 status 响应体)。"""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        resp = await client.get(
            f"/v1/admin/datasources/{name}/kb/distill-history/status")
        assert resp.status_code == 200
        st = resp.json()
        if st["status"] in ("done", "error"):
            return st
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError(f"distill not finished: {st}")
        await asyncio.sleep(0.05)


def _seed_query(store, *, question: str, sql: str = "SELECT 1",
                datasource: str, verdict: str = "OK", ts: str | None = None):
    return store.append_audit(
        ts=ts or now_iso(), user_id=None, username="bob",
        action="query.execute",
        details={"question": question, "sql": sql, "verdict": verdict,
                 "error": "", "datasource": datasource},
    )


async def _seed_refusal(store, *, question: str, reason: str = "uncovered",
                        miss: str = "", datasource: str = "demo",
                        ts: str | None = None):
    details = {"question": question, "datasource": datasource, "reason": reason}
    if miss:
        details["miss_reason"] = miss
    await store.append_audit(ts=ts or now_iso(), user_id=None, username="bob",
                             action="query.refused", details=details)


# ── 蒸馏管理端:任务生命周期 ─────────────────────────────


async def test_distill_lifecycle_done_with_summary(client, api_app, tmp_path):
    """202 → 轮询 → done:summary 有数,产物按 pending 门落盘,触发有审计。"""
    from trove.services.kb.init_tasks import init_tasks
    init_tasks.reset()
    await _register(client, "extra", tmp_path)
    # home 指向 tmp:episodes 读空目录,审计兜底就是下面这条种子
    api_app.state.config.home = str(tmp_path / "home")
    await _seed_query(api_app.state.auth.store,
                      question="students 有多少行?", datasource="extra")

    resp = await client.post(
        "/v1/admin/datasources/extra/kb/distill-history", json={"limit": 10})
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "running" and body["task_id"]

    st = await _wait_distill(client, "extra")
    assert st["status"] == "done" and st["progress"] == 100
    summary = st["summary"]
    assert summary["records"] == 1 and summary["examples"] == 1
    assert summary["dry_run"] is False and summary["limit"] == 10
    # 成功记录不是教训材料 → 没有 LLM 调用,lessons 0
    assert summary["lessons"] == 0 and summary["lessons_parse_failed"] == 0
    # pending 门:示例落盘但未进检索(确认前)—— 先同步镜像再查,排除
    # 「镜像没刷新」这种假阴性
    assert (api_app.state.kb.kb_dir / "extra" / "examples.yml").exists()
    assert await api_app.state.kb.list_pending_examples("extra")
    await api_app.state.kb.force_sync("extra")
    assert await api_app.state.kb.search_examples(
        "students 有多少行?", "extra") == []
    # 触发记审计
    entries = await api_app.state.auth.list_audit(action="kb.distill_history")
    assert len(entries) == 1
    assert entries[0]["details"]["name"] == "extra"
    assert entries[0]["details"]["examples"] == 1


async def test_distill_empty_history_is_task_failure(client, api_app, tmp_path):
    """空输入显式报「没有可蒸馏的历史」——不报成功,且零写入。"""
    from trove.services.kb.init_tasks import init_tasks
    init_tasks.reset()
    await _register(client, "empty-ds", tmp_path)
    api_app.state.config.home = str(tmp_path / "home")

    resp = await client.post(
        "/v1/admin/datasources/empty-ds/kb/distill-history", json={})
    assert resp.status_code == 202
    st = await _wait_distill(client, "empty-ds")
    assert st["status"] == "error"
    assert "没有可蒸馏的历史" in st["error"]
    assert not (api_app.state.kb.kb_dir / "empty-ds" / "examples.yml").exists()
    # 失败不记成功审计
    assert await api_app.state.auth.list_audit(action="kb.distill_history") == []


async def test_distill_mutex_and_status_kind_filter(client, api_app, tmp_path):
    """同源 running 任务 → 409;别人的任务不进我的状态(按 kind 过滤)。"""
    from trove.services.kb.init_tasks import init_tasks
    init_tasks.reset()
    await _register(client, "extra", tmp_path)

    # 同源已有 init 任务(共享注册表 = 同一数据源的 KB 写天然互斥)
    held = init_tasks.create("extra", kind="init")
    resp = await client.post(
        "/v1/admin/datasources/extra/kb/distill-history", json={})
    assert resp.status_code == 409
    # 状态按 kind 过滤:最近任务是 init → 蒸馏看到 idle
    st = (await client.get(
        "/v1/admin/datasources/extra/kb/distill-history/status")).json()
    assert st["status"] == "idle" and st["task_id"] is None
    init_tasks.fail(held["id"], "done for test")


async def test_distill_validates_input(client, api_app, tmp_path):
    from trove.services.kb.init_tasks import init_tasks
    init_tasks.reset()
    assert (await client.post(
        "/v1/admin/datasources/nope/kb/distill-history")).status_code == 404
    await _register(client, "extra", tmp_path)
    for bad in ({"limit": 0}, {"limit": "abc"}):
        resp = await client.post(
            "/v1/admin/datasources/extra/kb/distill-history", json=bad)
        assert resp.status_code == 400, bad


# ── 拒绝频率报表 ─────────────────────────────────────────


async def test_refusal_report_empty_is_explicit_zero(client, api_app):
    """空数据 = available + 全零结构(不是错误),coverage_note 必带。"""
    body = (await client.get("/v1/admin/audit/refusal-report")).json()
    assert body["available"] is True
    assert body["total"] == 0 and body["capped"] is False
    assert body["top_questions"] == []
    assert body["by_reason"] == [] and body["by_miss"] == []
    assert body["relationship_missing"]["count"] == 0
    assert body["relationship_missing"]["top_questions"] == []
    # 覆盖面自述:中英都有,且如实写明 MCP 在表内、直连编译不在
    assert body["coverage_note"] and "不含" in body["coverage_note"]
    assert "ask_data" in body["coverage_note_en"]
    assert "Not covered" in body["coverage_note_en"]


async def test_refusal_report_aggregates_and_singles_out_relations(
        client, api_app):
    store = api_app.state.auth.store
    await _seed_refusal(store, question="哪个地区贷款金额最高?", miss="no_metric_match")
    await _seed_refusal(store, question="哪个地区贷款金额最高?", miss="no_metric_match")
    await _seed_refusal(store, question="各分行存款余额", miss="unknown_cardinality")
    await _seed_refusal(store, question="各分行存款余额", miss="unknown_cardinality")
    await _seed_refusal(store, question="各分行存款余额", reason="no_model",
                        datasource="other")
    await _seed_refusal(store, question="第三个问题", reason="topic_out_of_scope")

    body = (await client.get("/v1/admin/audit/refusal-report?days=7")).json()
    assert body["available"] is True and body["total"] == 6
    assert body["capped"] is False
    assert body["by_reason"] == [
        {"reason": "uncovered", "count": 4},
        {"reason": "no_model", "count": 1},
        {"reason": "topic_out_of_scope", "count": 1},
    ]
    assert body["by_miss"] == [
        {"reason": "no_metric_match", "count": 2},
        {"reason": "unknown_cardinality", "count": 2},
    ]
    # 榜单:count 降序,question 升序稳定
    top = body["top_questions"]
    assert [t["question"] for t in top] == [
        "各分行存款余额", "哪个地区贷款金额最高?", "第三个问题"]
    assert top[0]["count"] == 3
    assert top[0]["reasons"] == ["no_model", "uncovered"]
    assert top[0]["datasources"] == ["demo", "other"]
    # 关系未声明单列:只数关系类**条目**(不把同问题的其它拒绝混进来)
    rel = body["relationship_missing"]
    assert rel["reasons"] == ["unknown_cardinality", "fan_out",
                              "ambiguous_join_path", "unreachable_table"]
    assert rel["count"] == 2
    assert len(rel["top_questions"]) == 1
    assert rel["top_questions"][0]["question"] == "各分行存款余额"
    assert rel["top_questions"][0]["count"] == 2


async def test_refusal_report_window_filters_old_rows(client, api_app):
    store = api_app.state.auth.store
    await _seed_refusal(store, question="新问题",
                        ts=now_iso())
    await _seed_refusal(store, question="远古问题",
                        ts="2000-01-01T00:00:00+00:00")
    body = (await client.get("/v1/admin/audit/refusal-report?days=7")).json()
    assert body["total"] == 1
    assert [t["question"] for t in body["top_questions"]] == ["新问题"]


async def test_refusal_report_degrades_instead_of_faking_zeros(
        client, api_app, monkeypatch):
    """审计面读不成 → available=False + null(绝不把「读不到」洗成 0)。"""
    async def _boom(**kwargs):
        raise RuntimeError("audit db down")

    monkeypatch.setattr(
        api_app.state.auth.store, "aggregate_refusal_audit", _boom)
    body = (await client.get("/v1/admin/audit/refusal-report")).json()
    assert body["available"] is False
    assert body["total"] is None
    assert body["relationship_missing"]["count"] is None
    assert "audit read failed" in body["note"]
    # 覆盖面的自述不因降级而缺席
    assert body["coverage_note"]


async def test_refusal_report_requires_privilege(anon_client, user_client):
    assert (await anon_client.get(
        "/v1/admin/audit/refusal-report")).status_code in (401, 403)
    assert (await user_client.get(
        "/v1/admin/audit/refusal-report")).status_code == 403
    assert (await user_client.post(
        "/v1/admin/datasources/x/kb/distill-history")).status_code == 403


async def test_overview_counts_unaffected_by_refusal_rows(client, api_app):
    """兼容安全带:query.refused 只喂拒绝报表,不动 overview 的既有计数。

    ``recent_events`` 是**事件流**——按设计收录所有 action,新的拒绝事件
    加入流里正是流在做本职;此处比较除它(与两个时间戳)之外的整份
    payload:usage / todos / datasources / wizard 逐字节不变。
    """
    def _stable(payload: dict) -> dict:
        payload = dict(payload)
        for k in ("generated_at", "elapsed_ms", "recent_events"):
            payload.pop(k, None)
        return payload

    first = _stable((await client.get("/v1/admin/overview")).json())
    await _seed_refusal(api_app.state.auth.store, question="被拒的问题",
                        miss="fan_out")
    second = _stable((await client.get("/v1/admin/overview")).json())
    assert first == second
