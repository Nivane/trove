"""服务编排 —— preflight、成本闸、后台运行、取消、草稿。

引擎用同形 fake(不碰真节点/真模型):这里钉的是**编排纪律** ——
成本确认、单实例单跑、中断不伪造完成、草稿只写 pending。
引擎自身的行为(零执行、结局分类)由 ``test_engine.py`` 负责。
"""

from __future__ import annotations

import asyncio

import pytest

from trove.core.config import AgentConfig
from trove.core.types import ColumnInfo, SchemaInfo, TableInfo
from trove.services.question_coverage import service as service_mod
from trove.services.question_coverage import taxonomy
from trove.services.question_coverage.service import (
    MAX_DRAFTS_PER_REQUEST,
    CoverageError,
    QuestionCoverageService,
)
from trove.services.question_coverage.store import CoverageRunActive, CoverageStore

DS = "demo"


# ── fakes ───────────────────────────────────────────────────────────────


class _KB:
    def __init__(self, semantics):
        self._semantics = semantics

    def semantics_path(self, datasource: str):
        return self._semantics


class _Adapter:
    def dialect(self) -> str:
        return "mysql"

    async def get_schema(self) -> SchemaInfo:
        return SchemaInfo(tables=[TableInfo(name="loan", columns=[
            ColumnInfo(name="amount", type="decimal")])])


class _Registry:
    def __init__(self):
        self.adapter = _Adapter()

    async def get(self, datasource=None):
        if datasource == "missing":
            raise RuntimeError("unknown datasource")
        return self.adapter


class _Provider:
    def __init__(self, model):
        self._model = model

    def model(self):
        return self._model


class _Det:
    def __init__(self, intent="query", *, certified=False, unmatched=False,
                 no_model=False):
        self.intent = intent
        self.certified = certified
        self.unmatched = unmatched
        self.no_model = no_model


def _result(seq: int, question: str, outcome: str, *, reason: str = "",
            llm_calls: int = 0, gaps=None, miss_reason: str = "",
            miss_class: str = "") -> dict:
    return {
        "seq": seq, "question": question, "outcome": outcome,
        "outcome_reason": reason, "intent": "query", "plan_typed": True,
        "miss_class": miss_class, "miss_reason": miss_reason,
        "miss_component": "", "template_name": "",
        "llm_calls": llm_calls, "elapsed_ms": 1,
        "gaps": list(gaps or []), "detail": {},
    }


class _Factory:
    """两件事:产出 fake 引擎 + 跨引擎共享的门/事件(取消测试用)。"""

    def __init__(self) -> None:
        self.engines: list[_Engine] = []
        self.dets: dict[str, _Det] = {}
        self.handlers: dict[str, object] = {}
        self.default_det = _Det()
        self.started = asyncio.Event()
        self.gate: asyncio.Event | None = None
        self.default_handler = lambda q, seq: _result(seq, q, "compiled")

    def __call__(self, **kwargs):
        engine = _Engine(self, **kwargs)
        self.engines.append(engine)
        return engine

    @property
    def last(self) -> "_Engine":
        return self.engines[-1]


class _Engine:
    def __init__(self, factory: _Factory, *, datasource="", run_id="",
                 user_id="") -> None:
        self.factory = factory
        self.datasource = datasource
        self.run_id = run_id
        self.user_id = user_id
        self.model = "fast/m"
        self.model_source = "model_fast"
        self.configured = False
        self.check_calls: list[tuple[str, int]] = []

    def configure(self, prepared) -> None:
        self.configured = True

    async def deterministic(self, question: str):
        return self.factory.dets.get(question, self.factory.default_det)

    async def check(self, question: str, *, seq: int):
        self.check_calls.append((question, seq))
        handler = self.factory.handlers.get(
            question, self.factory.default_handler)
        out = handler(question, seq)
        if asyncio.iscoroutine(out):
            out = await out
        return out


@pytest.fixture
async def svc(tmp_path, monkeypatch):
    store = CoverageStore(tmp_path, coverage_dir=tmp_path / "coverage")
    semantics = tmp_path / "semantics.yml"
    semantics.write_text("datasets: []\n", encoding="utf-8")
    factory = _Factory()
    holder = {"provider": _Provider(object())}

    async def fake_build(kb, config, datasource, adapter, *, schema=None,
                         root=None):
        return holder["provider"]

    monkeypatch.setattr(service_mod, "build_provider", fake_build)
    service = QuestionCoverageService(
        store, kb=_KB(semantics), connectors=_Registry(),
        llm=object(), config=AgentConfig(target="target/m", model_fast="fast/m"),
        engine_factory=factory, root=tmp_path,
        question_timeout_s=0.05, run_timeout_s=5.0,
    )
    service._factory = factory  # 测试句柄
    service._holder = holder  # 测试句柄
    yield service
    await service.dispose()


async def _seed(service, run_id, results, *, created_at, status="done",
                datasource=DS, digest="sha256:a", questions=None):
    questions = questions or [r["question"] for r in results]
    await service.store.create_run({
        "run_id": run_id, "datasource": datasource, "status": status,
        "created_at": created_at, "question_count": len(questions),
        "questions": questions, "taxonomy_version": 1,
        "semantic_digest": digest,
        "summary": {"source_label": "paste"},
    })
    for i, result in enumerate(results):
        await service.store.add_result(
            run_id, result, processed_count=i + 1)
    if status not in ("queued", "running"):
        await service.store.finish_run(
            run_id, status=status,
            summary=taxonomy.aggregate(results),
            processed_count=len(results))


# ── preflight(零 LLM) ──────────────────────────────────────────────────


async def test_preflight_counts_cost_and_model(svc):
    svc._factory.dets = {
        "plain": _Det(),
        "fast hit": _Det(certified=True),
        "unknown table": _Det(unmatched=True),
        "delete from loan": _Det(intent="write"),
        "how is the weather": _Det(intent="chitchat"),
    }
    preview = await svc.preflight(
        DS, ["plain", "fast hit", "unknown table",
             "delete from loan", "how is the weather"])
    assert preview["question_count"] == 5
    assert preview["llm_question_count"] == 3
    assert preview["not_query"] == 2
    assert preview["certified"] == 1
    assert preview["unmatched"] == 1
    assert preview["needs_plan"] == 1
    assert preview["estimated_calls"] == {"typical": 1, "cap": 16}
    assert preview["hard_call_cap"] == 16
    assert preview["model"] == "fast/m"
    assert preview["model_source"] == "model_fast"
    assert preview["semantic_digest"].startswith("sha256:")
    assert preview["blockers"] == []
    assert preview["active_run_id"] == ""
    # 零 LLM:preflight 只走确定性前段,引擎一次 check 都没跑。
    assert all(not e.check_calls for e in svc._factory.engines)


async def test_preflight_without_semantic_model_is_a_blocker(svc):
    svc._holder["provider"] = _Provider(None)
    preview = await svc.preflight(DS, ["q"])
    assert preview["blockers"] == [{
        "code": "no_semantic_model",
        "message": "semantic model is not initialized for this datasource",
        "href": "/admin/kb?ds=demo",
    }]


async def test_preflight_unknown_datasource(svc):
    with pytest.raises(CoverageError) as exc:
        await svc.preflight("missing", ["q"])
    assert exc.value.code == "datasource_not_found"
    assert exc.value.status == 400


async def test_preflight_unreachable_datasource(svc, monkeypatch):
    class _Broken(_Adapter):
        async def get_schema(self):
            raise TimeoutError("connect timeout")

    svc.connectors.adapter = _Broken()
    with pytest.raises(CoverageError) as exc:
        await svc.preflight(DS, ["q"])
    assert exc.value.code == "datasource_unreachable"


async def test_preflight_input_limits(svc):
    with pytest.raises(CoverageError) as exc:
        await svc.preflight(DS, [])
    assert exc.value.code == "no_questions" and exc.value.status == 400

    with pytest.raises(CoverageError) as exc:
        await svc.preflight(DS, ["q"] * 201)
    assert exc.value.code == "too_many_questions" and exc.value.status == 422

    with pytest.raises(CoverageError) as exc:
        await svc.preflight(DS, ["x" * 2001])
    assert exc.value.code == "question_too_long" and exc.value.status == 422


async def test_preflight_reports_active_run(svc):
    await _seed(svc, "active", [], created_at=taxonomy_now(), status="queued")
    preview = await svc.preflight(DS, ["q"])
    assert preview["active_run_id"] == "active"


def taxonomy_now() -> str:
    from trove.services.question_coverage.store import now_iso
    return now_iso()


# ── create_run ─────────────────────────────────────────────────────────


async def test_create_run_requires_explicit_cost_confirmation(svc):
    with pytest.raises(CoverageError) as exc:
        await svc.create_run(DS, ["q"])
    assert exc.value.code == "confirm_cost_required"
    assert await svc.store.active_run(DS) is None


async def test_create_run_refuses_without_semantic_model(svc):
    svc._holder["provider"] = _Provider(None)
    with pytest.raises(CoverageError) as exc:
        await svc.create_run(DS, ["q"], confirm_cost=True)
    assert exc.value.code == "no_semantic_model"
    assert exc.value.extra["href"] == "/admin/kb?ds=demo"


async def test_create_run_conflicts_with_active_run(svc):
    await _seed(svc, "active", [], created_at=taxonomy_now(), status="queued")
    with pytest.raises(CoverageError) as exc:
        await svc.create_run(DS, ["q"], confirm_cost=True)
    assert exc.value.code == "coverage_run_active"
    assert exc.value.status == 409
    assert exc.value.extra["run_id"] == "active"


async def test_create_run_conflicts_when_private_task_is_running(svc):
    task = svc.tasks.create(DS, kind="coverage")
    assert task is not None
    try:
        with pytest.raises(CoverageError) as exc:
            await svc.create_run(DS, ["q"], confirm_cost=True)
        assert exc.value.code == "coverage_run_active"
        assert await svc.store.active_run(DS) is None
    finally:
        svc.tasks.reset()


async def test_create_run_fails_the_task_on_store_conflict(svc, monkeypatch):
    async def conflict(run):
        raise CoverageRunActive("other")

    monkeypatch.setattr(svc.store, "create_run", conflict)
    with pytest.raises(CoverageError) as exc:
        await svc.create_run(DS, ["q"], confirm_cost=True)
    assert exc.value.code == "coverage_run_active"
    assert exc.value.extra["run_id"] == "other"
    task = svc.tasks.by_datasource(DS)
    assert task is not None and task["status"] == "error"


async def test_create_run_persists_and_completes(svc):
    reply = await svc.create_run(
        DS, ["q1", "  q2  ", "q3"], source_label="paste",
        confirm_cost=True, actor="admin")
    assert reply["status"] == "queued"
    assert reply["question_count"] == 3
    assert reply["estimated_calls"]["typical"] == 3
    assert reply["hard_call_cap"] == 3 * 6 + 10

    await svc.wait(reply["run_id"])
    run = await svc.store.get_run(reply["run_id"])
    assert run["status"] == "done"
    assert run["questions"] == ["q1", "q2", "q3"]
    assert run["processed_count"] == 3
    assert run["created_by"] == "admin"
    assert run["summary"]["source_label"] == "paste"
    assert run["summary"]["counts"]["compiled"] == 3
    assert run["summary"]["llm_calls_total"] == 0
    task = svc.tasks.get(reply["task_id"])
    assert task["status"] == "done"
    assert "3 题完成" in task["summary"]


# ── 运行:失败、超时、取消、预算 ────────────────────────────────────────


async def test_question_timeout_becomes_an_error_row_and_run_error(svc):
    reply = await svc.create_run(DS, ["a", "b", "c", "d"], confirm_cost=True)

    async def hang(question, seq):
        await asyncio.sleep(10)
        return _result(seq, question, "compiled")

    for q in ("a", "b", "c", "d"):
        svc._factory.handlers[q] = hang
    # 把超时压到毫秒级(默认 50ms 也会等 3 次,慢但不至于挂)
    svc.question_timeout_s = 0.005
    await svc.wait(reply["run_id"])

    run = await svc.store.get_run(reply["run_id"])
    assert run["status"] == "error"
    assert "consecutive" in run["error"]
    results = await svc.store.get_results(reply["run_id"])
    assert [r["outcome"] for r in results[:3]] == ["error", "error", "error"]
    assert all(r["outcome_reason"] == "timeout" for r in results[:3])
    assert results[3]["outcome"] == "skipped"
    assert results[3]["outcome_reason"] == "run_error"
    assert any(e.check_calls for e in svc._factory.engines)


async def test_cancel_fills_remaining_questions_with_skipped(svc):
    gate = asyncio.Event()
    svc._factory.gate = gate
    reply = await svc.create_run(DS, ["q1", "q2", "q3"], confirm_cost=True)

    async def slow(question, seq):
        svc._factory.started.set()
        await gate.wait()
        return _result(seq, question, "compiled")

    for q in ("q1", "q2", "q3"):
        svc._factory.handlers[q] = slow
    await asyncio.wait_for(svc._factory.started.wait(), timeout=1)
    cancel = await svc.cancel(reply["run_id"])
    assert cancel["status"] == "cancelling"
    gate.set()
    await svc.wait(reply["run_id"])

    run = await svc.store.get_run(reply["run_id"])
    assert run["status"] == "cancelled"
    results = await svc.store.get_results(reply["run_id"])
    assert [r["outcome"] for r in results] == ["compiled", "skipped", "skipped"]
    assert all(r["outcome_reason"] == "cancelled" for r in results[1:])
    assert run["summary"]["counts"]["skipped"] == 2


async def test_cancel_rejects_finished_runs(svc):
    reply = await svc.create_run(DS, ["q1"], confirm_cost=True)
    await svc.wait(reply["run_id"])
    with pytest.raises(CoverageError) as exc:
        await svc.cancel(reply["run_id"])
    assert exc.value.code == "run_not_active" and exc.value.status == 409


async def test_budget_exhaustion_stops_the_run_loudly(svc):
    reply = await svc.create_run(DS, ["q1", "q2"], confirm_cost=True)

    async def blow(question, seq):
        from trove.services.question_coverage.engine import CoverageBudgetExceeded
        raise CoverageBudgetExceeded(6)

    svc._factory.handlers["q1"] = blow
    await svc.wait(reply["run_id"])
    run = await svc.store.get_run(reply["run_id"])
    assert run["status"] == "error"
    assert "cap" in run["error"]
    results = await svc.store.get_results(reply["run_id"])
    assert [r["outcome"] for r in results] == ["skipped", "skipped"]
    assert results[0]["outcome_reason"] == "llm_budget_exceeded"


async def test_run_deadline_stops_after_the_current_question(svc):
    svc.run_timeout_s = 0.05
    svc.question_timeout_s = 1.0
    reply = await svc.create_run(DS, ["q1", "q2"], confirm_cost=True)

    async def slow(question, seq):
        await asyncio.sleep(0.08)
        return _result(seq, question, "compiled")

    svc._factory.handlers["q1"] = slow
    await svc.wait(reply["run_id"])
    run = await svc.store.get_run(reply["run_id"])
    assert run["status"] == "error"
    assert "timeout" in run["error"]
    results = await svc.store.get_results(reply["run_id"])
    assert results[0]["outcome"] == "compiled"  # 已完成的不丢
    assert results[1]["outcome"] == "skipped"
    assert results[1]["outcome_reason"] == "run_timeout"


async def test_single_question_exception_becomes_error_row(svc):
    reply = await svc.create_run(DS, ["bad", "good"], confirm_cost=True)

    async def boom(question, seq):
        raise ValueError("planner exploded")

    svc._factory.handlers["bad"] = boom
    await svc.wait(reply["run_id"])
    results = await svc.store.get_results(reply["run_id"])
    assert results[0]["outcome"] == "error"
    assert results[0]["outcome_reason"] == "node_error:ValueError"
    assert "planner exploded" in results[0]["detail"]["error"]
    assert results[1]["outcome"] == "compiled"


async def test_digest_and_model_changes_are_flagged(svc, tmp_path):
    semantics = tmp_path / "semantics.yml"
    reply = await svc.create_run(DS, ["q1"], confirm_cost=True)

    def change_semantics(question, seq):
        semantics.write_text("datasets: [changed]\n", encoding="utf-8")
        return _result(seq, question, "compiled")

    svc._factory.handlers["q1"] = change_semantics
    svc._configured_model = lambda: "other/model"
    await svc.wait(reply["run_id"])
    run = await svc.store.get_run(reply["run_id"])
    assert "semantic_changed_during_run" in run["summary"]["warnings"]
    assert "model_changed_during_run" in run["summary"]["warnings"]


async def test_high_unmeasured_warning_survives_finalize(svc):
    reply = await svc.create_run(DS, ["q1"], confirm_cost=True)

    def unplanned(question, seq):
        return _result(seq, question, "unplanned", reason="empty_plan")

    svc._factory.handlers["q1"] = unplanned
    await svc.wait(reply["run_id"])
    run = await svc.store.get_run(reply["run_id"])
    assert "high_unmeasured" in run["summary"]["warnings"]


async def test_run_level_exception_is_recorded_and_skips_remaining(svc, monkeypatch):
    reply = await svc.create_run(DS, ["q1", "q2"], confirm_cost=True)

    async def broken_get(run_id):
        raise RuntimeError("db exploded during progress")

    # 第一次取用于 mark_started 之前,跳过它;让循环里的第二次取抛错。
    calls = {"n": 0}
    original = svc.store.get_run

    async def flaky(run_id):
        calls["n"] += 1
        # 第 1 次 = _run 的初始读;第 2 次 = 循环里的进度读(要炸的那次);
        # 之后的读属于收尾,必须能继续 —— 收尾读也炸就只剩 stale 恢复了。
        if calls["n"] == 2:
            raise RuntimeError("db exploded during progress")
        return await original(run_id)

    monkeypatch.setattr(svc.store, "get_run", flaky)
    await svc.wait(reply["run_id"])
    run = await original(reply["run_id"])
    assert run["status"] == "error"
    assert "db exploded" in run["error"]


async def test_startup_reconciles_stale_runs(svc):
    await svc.store.create_run({
        "run_id": "stale", "datasource": DS, "status": "running",
        "created_at": "2020-01-01T00:00:00+00:00",
        "question_count": 2, "questions": ["q1", "q2"],
        "taxonomy_version": 1,
    })
    await svc.startup()
    run = await svc.store.get_run("stale")
    assert run["status"] == "interrupted"
    results = await svc.store.get_results("stale")
    assert [r["outcome"] for r in results] == ["skipped", "skipped"]


# ── status / report / list / diff ──────────────────────────────────────


async def test_status_shape_and_terminal_summary(svc):
    reply = await svc.create_run(DS, ["q1", "q2"], confirm_cost=True)
    await svc.wait(reply["run_id"])
    status = await svc.status(reply["run_id"])
    assert status["status"] == "done"
    assert status["question_count"] == 2
    assert status["processed_count"] == 2
    assert status["counts"]["compiled"] == 2
    assert status["llm_calls_total"] == 0
    assert status["summary"]["metrics"]["deterministic_coverage"]["rate"] == 1.0
    assert status["task"]["status"] == "done"


async def test_status_unknown_run(svc):
    with pytest.raises(CoverageError) as exc:
        await svc.status("nope")
    assert exc.value.code == "run_not_found" and exc.value.status == 404


async def test_report_carries_gap_groups(svc):
    reply = await svc.create_run(DS, ["q1"], confirm_cost=True)

    def gap(question, seq):
        return _result(seq, question, "refused", gaps=[{
            "kind": "relation", "reason": "unknown_cardinality",
            "component": "loan, account", "value": "", "draftable": False,
            "workbench": "/admin/semantic?ds=demo&tab=datasets&q=loan",
            "specs": [], "label": {"zh": "关系未声明", "en": "relation"},
        }])

    svc._factory.handlers["q1"] = gap
    svc._current_model = lambda datasource: None
    await svc.wait(reply["run_id"])
    report = await svc.report(reply["run_id"])
    assert report["run"]["run_id"] == reply["run_id"]
    assert report["summary"]["counts"]["refused"] == 1
    assert report["results"][0]["outcome"] == "refused"
    assert report["gap_groups"][0]["reason"] == "unknown_cardinality"
    assert report["gap_groups"][0]["count"] == 1


async def test_list_runs_attaches_previous_done_run(svc):
    await _seed(svc, "r1", [_result(0, "q1", "compiled")],
                created_at="2026-10-18T10:00:00+00:00")
    await _seed(svc, "r2", [_result(0, "q1", "refused")],
                created_at="2026-10-18T11:00:00+00:00")
    listing = await svc.list_runs(DS)
    assert [r["run_id"] for r in listing["runs"]] == ["r2", "r1"]
    assert "questions" not in listing["runs"][0]
    assert (
        {r["run_id"]: r["previous_done_run_id"] for r in listing["runs"]}
        == {"r2": "r1", "r1": ""}
    )
    filtered = await svc.list_runs(DS, status="error")
    assert filtered["runs"] == []


async def test_diff_directions_and_summary_delta(svc):
    await _seed(svc, "base", [
        _result(0, "q1", "refused", reason="x"),
        _result(1, "q2", "compiled"),
    ], created_at="2026-10-18T10:00:00+00:00")
    await _seed(svc, "current", [
        _result(0, "q1", "compiled"),
        _result(1, "q2", "refused", reason="y"),
    ], created_at="2026-10-18T11:00:00+00:00")
    body = await svc.diff("current")
    assert body["base_run_id"] == "base"
    assert body["matched"] == 2
    assert body["changes"]["improved"] == 1
    assert body["changes"]["regressed"] == 1
    assert body["semantic_changed"] is False
    delta = body["summary_delta"]["deterministic_coverage"]
    assert delta["base"]["rate"] == 0.5
    assert delta["current"]["rate"] == 0.5
    assert delta["delta"] == 0.0


async def test_diff_without_previous_run(svc):
    await _seed(svc, "only", [_result(0, "q1", "compiled")],
                created_at="2026-10-18T10:00:00+00:00")
    body = await svc.diff("only")
    assert body["reason"] == "no_previous_run"
    assert body["base_run_id"] == ""
    assert body["changes"]["improved"] == 0


async def test_diff_rejects_same_or_mismatched_runs(svc):
    await _seed(svc, "a", [_result(0, "q1", "compiled")],
                created_at="2026-10-18T10:00:00+00:00")
    with pytest.raises(CoverageError) as exc:
        await svc.diff("a", base_run_id="a")
    assert exc.value.code == "diff_same_run"

    await _seed(svc, "b", [_result(0, "q1", "compiled")],
                created_at="2026-10-18T11:00:00+00:00", datasource="other")
    with pytest.raises(CoverageError) as exc:
        await svc.diff("a", base_run_id="b")
    assert exc.value.code == "diff_datasource_mismatch"


async def test_diff_semantic_changed_flag(svc):
    await _seed(svc, "base", [_result(0, "q1", "compiled")],
                created_at="2026-10-18T10:00:00+00:00", digest="sha256:a")
    await _seed(svc, "current", [_result(0, "q1", "compiled")],
                created_at="2026-10-18T11:00:00+00:00", digest="sha256:b")
    body = await svc.diff("current")
    assert body["semantic_changed"] is True
    assert body["base_semantic_digest"] == "sha256:a"


# ── 缺口 → pending 草稿 ────────────────────────────────────────────────


def _gap_row(reason="no_metric_match", component="loan.total", *,
             draftable=True, value=""):
    return {
        "kind": "metric" if reason == "no_metric_match" else "other",
        "reason": reason, "component": component, "value": value,
        "draftable": draftable, "specs": [], "workbench": "",
        "label": {"zh": reason, "en": reason},
    }


@pytest.fixture
def drafts(monkeypatch):
    """把候选权威与建草稿写路径换成可观测 fake(编排级测试)。"""
    recorded = {"specs": None, "kwargs": None, "calls": 0}

    def fake_candidates(misses, model):
        out = []
        for miss in misses:
            if model is None:
                return []
            if miss.get("reason") != "no_metric_match":
                continue
            if str(miss.get("component") or "").startswith("gone."):
                continue
            out.append({
                "kind": "metric",
                "name": str(miss["component"]).split(".")[-1],
                "payload": {"name": str(miss["component"]).split(".")[-1]},
                "reason": miss.get("reason") or "",
                "evidence": miss.get("component") or "",
            })
        return out

    async def fake_create_specs(kb, datasource, specs, **kwargs):
        recorded["calls"] += 1
        recorded["specs"] = list(specs)
        recorded["kwargs"] = kwargs
        out = []
        for spec in specs:
            name = spec["name"]
            if name == "dropped":
                continue  # 模拟单次上限截断
            if name == "bad":
                out.append({"kind": spec["kind"], "name": name,
                            "reason": spec.get("reason", ""),
                            "status": "invalid", "draft_id": "",
                            "errors": ["invalid payload"]})
            elif name == "dup":
                out.append({"kind": spec["kind"], "name": name,
                            "reason": "", "status": "exists",
                            "draft_id": "d1", "errors": []})
            else:
                out.append({"kind": spec["kind"], "name": name,
                            "reason": "", "status": "created",
                            "draft_id": "new-id", "errors": []})
        return out

    monkeypatch.setattr(service_mod, "candidate_specs", fake_candidates)
    monkeypatch.setattr(service_mod, "create_specs", fake_create_specs)
    return recorded


async def _seed_with_gaps(svc, gaps_by_question, *, run_id="draftable",
                          created_at="2026-10-18T10:00:00+00:00"):
    results = [
        _result(seq, question, "refused", gaps=gaps)
        for seq, (question, gaps) in enumerate(gaps_by_question.items())
    ]
    await _seed(svc, run_id, results, created_at=created_at)
    svc._current_model = lambda datasource: object()
    return run_id


async def test_draft_gaps_creates_pending_drafts(svc, drafts):
    run_id = await _seed_with_gaps(svc, {
        "q1": [_gap_row(), _gap_row(reason="unknown_cardinality",
                                    component="loan, account", draftable=False)],
    })
    body = await svc.draft_gaps(run_id, seqs=[0], actor="admin")
    assert body["created"] == 1
    assert body["not_draftable"] == 1
    assert body["truncated"] is False
    created = [r for r in body["results"] if r["status"] == "created"]
    assert created[0]["name"] == "total"
    assert created[0]["draft_id"] == "new-id"
    assert created[0]["seq"] == 0
    # 唯一写路径:validate=True(与 confirm 同一条应用路径的干跑),上限 50。
    assert drafts["calls"] == 1
    assert drafts["kwargs"]["validate"] is True
    assert drafts["kwargs"]["max_candidates"] == MAX_DRAFTS_PER_REQUEST
    assert drafts["kwargs"]["note_prefix"] == "coverage"
    assert drafts["kwargs"]["actor"] == "admin"
    assert drafts["kwargs"]["dialect"] == "mysql"
    # 客户端的 payload 从不参与:specs 全部由当前模型重算。
    assert drafts["specs"][0]["question"] == "q1"


async def test_draft_gaps_dedupes_assets_but_reports_every_origin(svc, drafts):
    run_id = await _seed_with_gaps(svc, {
        "q1": [_gap_row()],
        "q2": [_gap_row()],
    })
    body = await svc.draft_gaps(run_id, all=True)
    assert len(drafts["specs"]) == 1  # 同一 (kind,name) 只建一次
    assert [r["status"] for r in body["results"]] == ["created", "created"]
    assert [r["seq"] for r in body["results"]] == [0, 1]


async def test_draft_gaps_statuses_exists_invalid_and_stale(svc, drafts):
    run_id = await _seed_with_gaps(svc, {
        "q1": [_gap_row(component="loan.dup")],
        "q2": [_gap_row(component="loan.bad")],
        "q3": [_gap_row(component="gone.total")],
    })
    body = await svc.draft_gaps(run_id, all=True)
    by_seq = {r["seq"]: r for r in body["results"]}
    assert by_seq[0]["status"] == "exists"
    assert by_seq[0]["draft_id"] == "d1"
    assert by_seq[1]["status"] == "invalid"
    assert by_seq[1]["errors"] == ["invalid payload"]
    # run 时标过 draftable,但当前模型已给不出规格 → stale(不是 not_draftable)
    assert by_seq[2]["status"] == "stale"
    assert body["counts"] == {
        "created": 0, "exists": 1, "invalid": 1, "not_draftable": 0, "stale": 1}


async def test_draft_gaps_truncation_is_visible(svc, drafts):
    run_id = await _seed_with_gaps(svc, {"q1": [_gap_row(component="loan.dropped")]})
    body = await svc.draft_gaps(run_id, all=True)
    assert body["truncated"] is True
    assert body["truncated_assets"] == 1
    assert body["results"] == []


async def test_draft_gaps_selection_validation(svc, drafts):
    run_id = await _seed_with_gaps(svc, {"q1": [_gap_row()]})
    with pytest.raises(CoverageError) as exc:
        await svc.draft_gaps(run_id)
    assert exc.value.code == "no_selection"

    with pytest.raises(CoverageError) as exc:
        await svc.draft_gaps(run_id, seqs=[0], all=True)
    assert exc.value.code == "no_selection"

    with pytest.raises(CoverageError) as exc:
        await svc.draft_gaps(run_id, seqs=[9])
    assert exc.value.code == "unknown_seqs"
    assert exc.value.extra["seqs"] == [9]

    with pytest.raises(CoverageError) as exc:
        await svc.draft_gaps(run_id, seqs=list(range(51)))
    assert exc.value.code == "too_many_seqs" and exc.value.status == 422


async def test_draft_gaps_requires_a_finished_run(svc, drafts):
    await _seed(svc, "active", [], created_at=taxonomy_now(), status="queued")
    with pytest.raises(CoverageError) as exc:
        await svc.draft_gaps("active", all=True)
    assert exc.value.code == "run_not_finished" and exc.value.status == 409


async def test_draft_gaps_never_confirms(svc):
    # 结构性边界:服务模块不持有任何确认入口,只有 create_specs 一条写路径。
    assert "confirm_draft" not in service_mod.__dict__
    assert "SemanticManager" not in service_mod.__dict__


async def test_dispose_cancels_background_runs(tmp_path, monkeypatch):
    store = CoverageStore(tmp_path, coverage_dir=tmp_path / "coverage")
    semantics = tmp_path / "semantics.yml"
    semantics.write_text("datasets: []\n", encoding="utf-8")
    factory = _Factory()
    started = asyncio.Event()

    async def hang(question, seq):
        started.set()
        await asyncio.sleep(3600)
        return _result(seq, question, "compiled")

    factory.handlers["q1"] = hang

    async def fake_build(kb, config, datasource, adapter, *, schema=None,
                         root=None):
        return _Provider(object())

    monkeypatch.setattr(service_mod, "build_provider", fake_build)
    service = QuestionCoverageService(
        store, kb=_KB(semantics), connectors=_Registry(), llm=object(),
        config=AgentConfig(target="t/m", model_fast="fast/m"),
        engine_factory=factory, root=tmp_path,
    )
    reply = await service.create_run(DS, ["q1"], confirm_cost=True)
    await asyncio.wait_for(started.wait(), timeout=1)
    # 直接取消后台任务(与 dispose 的第一拍相同),先读回状态,
    # 再走 dispose —— store 一旦 dispose,后端连接就关了。
    task = service._background[reply["run_id"]]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    run = await store.get_run(reply["run_id"])
    assert run["status"] == "interrupted"
    results = await store.get_results(reply["run_id"])
    assert [r["outcome"] for r in results] == ["skipped"]
    assert results[0]["outcome_reason"] == "interrupted"
    await service.dispose()
