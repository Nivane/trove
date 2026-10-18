"""覆盖率体检 admin API —— 门禁、错误码、multipart、审计与端到端闭环。

服务编排的细节(成本闸、中断语义、草稿重算)由
``tests/services/question_coverage`` 负责;这里钉的是 HTTP 契约:
- 全部端点 ``require_admin``(非 admin 403,未装配 503);
- ``CoverageError`` → 稳定 ``code`` + 恰当状态码;
- CSV 走标准 multipart(表单字段 ``question_column``),有界读取;
- 审计只留元数据(run_id/计数/资产名),**没有问题原文**;
- 建 run → 后台完成 → status/report/diff → 草稿 → 取消。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from trove.api.app import create_app
from trove.core.config import AgentConfig
from trove.core.types import ColumnInfo, SchemaInfo, TableInfo
from trove.services.question_coverage import service as service_mod
from trove.services.question_coverage import taxonomy
from trove.services.question_coverage.csv_input import MAX_CSV_BYTES
from trove.services.question_coverage.service import QuestionCoverageService
from trove.services.question_coverage.store import CoverageStore

DS = "demo"


# ── fakes(引擎同形替身;不碰真节点/真模型) ─────────────────────────────


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
            gaps=None, miss_reason: str = "") -> dict:
    return {
        "seq": seq, "question": question, "outcome": outcome,
        "outcome_reason": reason, "intent": "query", "plan_typed": True,
        "miss_class": "soft" if outcome == "soft_miss" else "",
        "miss_reason": miss_reason, "miss_component": "",
        "template_name": "", "llm_calls": 0, "elapsed_ms": 1,
        "gaps": list(gaps or []), "detail": {},
    }


class _Factory:
    def __init__(self) -> None:
        self.engines: list[_Engine] = []
        self.dets: dict[str, _Det] = {}
        self.default_det = _Det()
        self.default_handler = lambda q, seq: _result(seq, q, "compiled")

    def __call__(self, **kwargs):
        engine = _Engine(self, **kwargs)
        self.engines.append(engine)
        return engine


class _Engine:
    def __init__(self, factory: _Factory, *, datasource="", run_id="",
                 user_id="") -> None:
        self.factory = factory
        self.datasource = datasource
        self.run_id = run_id
        self.model = "fast/m"
        self.model_source = "model_fast"

    def configure(self, prepared) -> None:
        pass

    async def deterministic(self, question: str):
        return self.factory.dets.get(question, self.factory.default_det)

    async def check(self, question: str, *, seq: int):
        return self.factory.default_handler(question, seq)


# ── fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
async def auth_service(tmp_path):
    from trove.services.auth.service import AuthService

    auth = AuthService(tmp_path / "app.db")
    await auth.ensure_bootstrap_admin(env_password="adminpw")
    await auth.create_user("bob", "bobpw", display_name="Bob")
    yield auth
    await auth.dispose()


@pytest.fixture
async def admin_token(auth_service):
    admin = await auth_service.authenticate("admin", "adminpw")
    raw, _ = await auth_service.create_token(admin["id"], label="test-admin")
    return raw


@pytest.fixture
async def user_token(auth_service):
    bob = await auth_service.authenticate("bob", "bobpw")
    raw, _ = await auth_service.create_token(bob["id"], label="test-bob")
    return raw


@pytest.fixture
async def coverage_service(tmp_path, monkeypatch):
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
        llm=object(),
        config=AgentConfig(target="target/m", model_fast="fast/m"),
        engine_factory=factory, root=tmp_path,
        question_timeout_s=2.0, run_timeout_s=10.0,
    )
    service._factory = factory
    service._holder = holder
    yield service
    await service.dispose()


@pytest.fixture
async def coverage_app(auth_service, coverage_service):
    app = create_app({
        "session_manager": None,
        "kb": _KB(None),
        "auth": auth_service,
        "question_coverage": coverage_service,
    })
    return app


@pytest.fixture
async def client(coverage_app, admin_token):
    transport = ASGITransport(app=coverage_app)
    headers = {"Authorization": f"Bearer {admin_token}"}
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=headers,
    ) as c:
        yield c


@pytest.fixture
async def user_client(coverage_app, user_token):
    transport = ASGITransport(app=coverage_app)
    headers = {"Authorization": f"Bearer {user_token}"}
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=headers,
    ) as c:
        yield c


def _now(offset_minutes: int = 0) -> str:
    moment = datetime.now(timezone.utc) + timedelta(minutes=offset_minutes)
    return moment.isoformat()


async def _seed(service, run_id, results, *, created_at, status="done",
                datasource=DS, questions=None):
    questions = questions or [r["question"] for r in results]
    await service.store.create_run({
        "run_id": run_id, "datasource": datasource, "status": status,
        "created_at": created_at,
        "heartbeat_at": created_at if status in ("queued", "running") else None,
        "question_count": len(questions), "questions": questions,
        "taxonomy_version": 1, "semantic_digest": "sha256:a",
        "summary": {"source_label": "paste"},
    })
    for i, result in enumerate(results):
        await service.store.add_result(
            run_id, result, processed_count=i + 1, at=created_at)
    if status not in ("queued", "running"):
        await service.store.finish_run(
            run_id, status=status, summary=taxonomy.aggregate(results),
            processed_count=len(results))


# ── 门禁 ────────────────────────────────────────────────────────────────


async def test_non_admin_is_forbidden(user_client):
    r = await user_client.get("/v1/admin/question-coverage/runs",
                              params={"datasource": DS})
    assert r.status_code == 403


async def test_unconfigured_service_is_503(auth_service, admin_token):
    app = create_app({
        "session_manager": None, "kb": _KB(None), "auth": auth_service,
    })
    transport = ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {admin_token}"}
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=headers,
    ) as c:
        r = await c.post(
            "/v1/admin/question-coverage/preflight",
            json={"datasource": DS, "questions": ["q"]})
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "coverage_not_configured"


# ── preflight ───────────────────────────────────────────────────────────


async def test_preflight_reports_counts_cost_and_model(client, coverage_service):
    coverage_service._factory.dets = {
        "plain": _Det(),
        "fast hit": _Det(certified=True),
        "unknown": _Det(unmatched=True),
        "delete from loan": _Det(intent="write"),
    }
    r = await client.post(
        "/v1/admin/question-coverage/preflight",
        json={"datasource": DS, "questions": [
            "plain", "fast hit", "unknown", "delete from loan"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["question_count"] == 4
    assert body["not_query"] == 1
    assert body["certified"] == 1
    assert body["unmatched"] == 1
    assert body["needs_plan"] == 1
    assert body["estimated_calls"] == {"typical": 1, "cap": 6 + 10}
    assert body["hard_call_cap"] == 16
    assert body["model"] == "fast/m"
    assert body["model_source"] == "model_fast"
    assert body["blockers"] == []
    assert body["active_run_id"] == ""


async def test_preflight_reports_no_model_blocker(client, coverage_service):
    coverage_service._holder["provider"] = None
    r = await client.post(
        "/v1/admin/question-coverage/preflight",
        json={"datasource": "待建档", "questions": ["q"]})
    assert r.status_code == 200, r.text
    blockers = r.json()["blockers"]
    assert blockers[0]["code"] == "no_semantic_model"
    assert blockers[0]["href"].startswith("/admin/kb?ds=")


async def test_preflight_unknown_datasource_is_400(client):
    r = await client.post(
        "/v1/admin/question-coverage/preflight",
        json={"datasource": "missing", "questions": ["q"]})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "datasource_not_found"


async def test_preflight_empty_questions_is_400(client):
    r = await client.post(
        "/v1/admin/question-coverage/preflight",
        json={"datasource": DS, "questions": ["", "  "]})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "no_questions"


# ── parse-csv(multipart) ────────────────────────────────────────────────


async def test_parse_csv_multipart_autodetect_header(client):
    data = "﻿question,owner\n哪个地区贷款最多?,a\n多少笔贷款?,b\n".encode("utf-8")
    r = await client.post(
        "/v1/admin/question-coverage/parse-csv",
        files={"file": ("q.csv", data, "text/csv")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["questions"] == ["哪个地区贷款最多?", "多少笔贷款?"]
    assert body["header"] == "question"
    assert body["source_label"] == "q.csv"
    assert body["encoding"] == "utf-8-sig"


async def test_parse_csv_explicit_column_form_field(client):
    data = "owner,text\nalice,第一题\nbob,第二题\n".encode("utf-8")
    r = await client.post(
        "/v1/admin/question-coverage/parse-csv",
        files={"file": ("q.csv", data, "text/csv")},
        data={"question_column": "text"})
    assert r.status_code == 200, r.text
    assert r.json()["questions"] == ["第一题", "第二题"]


async def test_parse_csv_rejects_bad_inputs(client):
    empty = await client.post(
        "/v1/admin/question-coverage/parse-csv",
        files={"file": ("empty.csv", b"", "text/csv")})
    assert empty.status_code == 400
    assert empty.json()["detail"]["code"] == "empty_file"

    nul = await client.post(
        "/v1/admin/question-coverage/parse-csv",
        files={"file": ("bad.csv", b"question\n\x00oops\n", "text/csv")})
    assert nul.status_code == 400
    assert nul.json()["detail"]["code"] == "invalid_csv"

    missing = await client.post(
        "/v1/admin/question-coverage/parse-csv",
        files={"file": ("q.csv", b"question\nq\n", "text/csv")},
        data={"question_column": "nope"})
    assert missing.status_code == 400
    assert missing.json()["detail"]["code"] == "question_column_not_found"

    big = await client.post(
        "/v1/admin/question-coverage/parse-csv",
        files={"file": ("big.csv", b"q\n" * (MAX_CSV_BYTES // 2 + 1),
                        "text/csv")})
    assert big.status_code == 413
    assert big.json()["detail"]["code"] == "file_too_large"


# ── 建 run / 轮询 / 报告 / diff ─────────────────────────────────────────


async def test_create_run_requires_explicit_cost_confirmation(client):
    r = await client.post(
        "/v1/admin/question-coverage/runs",
        json={"datasource": DS, "questions": ["q"], "confirm_cost": False})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "confirm_cost_required"


async def test_create_run_runs_to_completion_and_is_audited(
    client, coverage_service, auth_service,
):
    r = await client.post(
        "/v1/admin/question-coverage/runs",
        json={"datasource": DS, "questions": ["q1", "q2"],
              "confirm_cost": True, "source_label": "paste"})
    assert r.status_code == 202, r.text
    payload = r.json()
    run_id = payload["run_id"]
    assert payload["status"] == "queued"
    assert payload["question_count"] == 2

    await coverage_service.wait(run_id)
    status = (await client.get(
        f"/v1/admin/question-coverage/runs/{run_id}/status")).json()
    assert status["status"] == "done"
    assert status["processed_count"] == 2
    assert status["counts"]["compiled"] == 2
    assert status["llm_calls_total"] == 0
    assert status["summary"]["question_total"] == 2

    report = (await client.get(
        f"/v1/admin/question-coverage/runs/{run_id}")).json()
    assert report["run"]["run_id"] == run_id
    assert [row["outcome"] for row in report["results"]] == [
        "compiled", "compiled"]

    rows = await auth_service.list_audit(action="coverage.run.create")
    assert len(rows) == 1
    details = rows[0]["details"]
    assert details["run_id"] == run_id
    assert details["question_count"] == 2
    dumped = json.dumps(details, ensure_ascii=False)
    assert "q1" not in dumped and "q2" not in dumped, "审计不记问题原文"


async def test_create_run_conflicts_with_active_run(
    client, coverage_service,
):
    await _seed(coverage_service, "active-1", [_result(1, "q", "compiled")],
                created_at=_now(), status="running")
    r = await client.post(
        "/v1/admin/question-coverage/runs",
        json={"datasource": DS, "questions": ["q"], "confirm_cost": True})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "coverage_run_active"
    assert detail["run_id"] == "active-1"


async def test_create_run_refuses_without_semantic_model(
    client, coverage_service,
):
    coverage_service._holder["provider"] = None
    r = await client.post(
        "/v1/admin/question-coverage/runs",
        json={"datasource": DS, "questions": ["q"], "confirm_cost": True})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["code"] == "no_semantic_model"
    assert detail["href"].startswith("/admin/kb?ds=")


async def test_status_and_report_404(client):
    for path in ("/runs/nope/status", "/runs/nope"):
        r = await client.get(f"/v1/admin/question-coverage{path}")
        assert r.status_code == 404
        assert r.json()["detail"]["code"] == "run_not_found"


async def test_stale_running_run_reports_interrupted(
    client, coverage_service,
):
    await _seed(
        coverage_service, "stale-1", [],
        created_at=_now(-20), status="running",
        questions=["only question"])
    status = (await client.get(
        "/v1/admin/question-coverage/runs/stale-1/status")).json()
    assert status["status"] == "interrupted"
    assert status["counts"]["skipped"] == 1


async def test_list_runs_validates_status_and_returns_previous_pointer(
    client, coverage_service,
):
    await _seed(coverage_service, "r1", [_result(1, "q", "compiled")],
                created_at=_now(-60))
    await _seed(coverage_service, "r2", [_result(1, "q", "refused")],
                created_at=_now())

    bad = await client.get(
        "/v1/admin/question-coverage/runs",
        params={"datasource": DS, "status": "bogus"})
    assert bad.status_code == 400
    assert bad.json()["detail"]["code"] == "invalid_status"
    assert "done" in bad.json()["detail"]["allowed"]

    body = (await client.get(
        "/v1/admin/question-coverage/runs",
        params={"datasource": DS})).json()
    assert [run["run_id"] for run in body["runs"]] == ["r2", "r1"]
    assert body["runs"][0]["previous_done_run_id"] == "r1"
    assert "questions" not in body["runs"][0]


async def test_diff_defaults_to_previous_done_run(
    client, coverage_service,
):
    await _seed(coverage_service, "base", [_result(1, "how many", "refused")],
                created_at=_now(-60))
    await _seed(coverage_service, "curr", [_result(1, "how many", "compiled")],
                created_at=_now())

    body = (await client.get(
        "/v1/admin/question-coverage/runs/curr/diff")).json()
    assert body["base_run_id"] == "base"
    assert body["changes"]["improved"] == 1
    assert body["summary_delta"]["deterministic_coverage"]["delta"] > 0

    same = await client.get(
        "/v1/admin/question-coverage/runs/curr/diff",
        params={"base_run_id": "curr"})
    assert same.status_code == 400
    assert same.json()["detail"]["code"] == "diff_same_run"


async def test_diff_without_previous_run_says_so(client, coverage_service):
    await _seed(coverage_service, "solo", [_result(1, "q", "compiled")],
                created_at=_now())
    body = (await client.get(
        "/v1/admin/question-coverage/runs/solo/diff")).json()
    assert body["reason"] == "no_previous_run"
    assert body["base_run_id"] == ""


async def test_diff_refuses_active_current_run(client, coverage_service):
    await _seed(coverage_service, "busy", [],
                created_at=_now(), status="running",
                questions=["q"])
    r = await client.get("/v1/admin/question-coverage/runs/busy/diff")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "run_not_finished"


# ── 草稿 ────────────────────────────────────────────────────────────────


async def test_drafts_create_pending_and_are_audited_without_questions(
    client, coverage_service, auth_service, monkeypatch,
):
    gap = {
        "kind": "metric", "reason": "no_metric_match",
        "component": "loan_count", "value": "", "label": {"zh": "缺指标"},
        "draftable": True, "specs": [], "workbench": "",
    }
    await _seed(
        coverage_service, "run-drafts",
        [_result(1, "多少笔贷款", "soft_miss",
                 miss_reason="no_metric_match", gaps=[gap])],
        created_at=_now())

    calls: dict = {}

    def fake_specs(misses, model):
        if misses and misses[0].get("reason") == "no_metric_match":
            return [{"kind": "metric", "name": "loan_count",
                     "reason": "no_metric_match", "payload": {}}]
        return []

    async def fake_create(kb, ds, specs, **kwargs):
        calls["specs"] = specs
        calls["kwargs"] = kwargs
        return [{"kind": spec["kind"], "name": spec["name"],
                 "reason": spec["reason"], "status": "created",
                 "draft_id": "d-1", "errors": []} for spec in specs]

    monkeypatch.setattr(service_mod, "candidate_specs", fake_specs)
    monkeypatch.setattr(service_mod, "create_specs", fake_create)

    r = await client.post(
        "/v1/admin/question-coverage/runs/run-drafts/drafts",
        json={"seqs": [1]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["counts"]["created"] == 1
    assert body["results"][0]["name"] == "loan_count"
    assert body["results"][0]["draft_id"] == "d-1"

    # 草稿只落 pending:走 create_specs 的唯一写路径,validate 打开,
    # note 前缀是 coverage,actor 是当前 admin。
    assert calls["kwargs"]["validate"] is True
    assert calls["kwargs"]["note_prefix"] == "coverage"
    assert calls["kwargs"]["actor"] == "admin"
    assert calls["kwargs"]["dialect"] == "mysql"

    rows = await auth_service.list_audit(action="coverage.drafts.request")
    assert len(rows) == 1
    assert rows[0]["details"]["assets"] == ["metric:loan_count"]
    assert "多少笔贷款" not in json.dumps(rows[0]["details"], ensure_ascii=False)


async def test_drafts_selection_validation(client, coverage_service):
    await _seed(coverage_service, "sel", [_result(1, "q", "compiled")],
                created_at=_now())
    base = "/v1/admin/question-coverage/runs/sel/drafts"

    empty = await client.post(base, json={})
    assert empty.status_code == 400
    assert empty.json()["detail"]["code"] == "no_selection"

    both = await client.post(base, json={"seqs": [1], "all": True})
    assert both.status_code == 400

    unknown = await client.post(base, json={"seqs": [99]})
    assert unknown.status_code == 400
    assert unknown.json()["detail"]["code"] == "unknown_seqs"
    assert unknown.json()["detail"]["seqs"] == [99]

    too_many = await client.post(base, json={"seqs": list(range(1, 60))})
    assert too_many.status_code == 422
    assert too_many.json()["detail"]["code"] == "too_many_seqs"


async def test_drafts_refuse_active_run(client, coverage_service):
    await _seed(coverage_service, "busy-drafts", [],
                created_at=_now(), status="running", questions=["q"])
    r = await client.post(
        "/v1/admin/question-coverage/runs/busy-drafts/drafts",
        json={"all": True})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "run_not_finished"


# ── 取消 ────────────────────────────────────────────────────────────────


async def test_cancel_requests_and_is_audited(
    client, coverage_service, auth_service,
):
    await _seed(coverage_service, "cancel-1", [],
                created_at=_now(), status="running", questions=["q"])

    r = await client.post(
        "/v1/admin/question-coverage/runs/cancel-1/cancel")
    assert r.status_code == 202, r.text
    assert r.json() == {"run_id": "cancel-1", "status": "cancelling"}

    status = (await client.get(
        "/v1/admin/question-coverage/runs/cancel-1/status")).json()
    assert status["cancel_requested"] is True

    # 取消是幂等的:重复请求仍是 202(仍在跑,旗子已经插上)。
    again = await client.post(
        "/v1/admin/question-coverage/runs/cancel-1/cancel")
    assert again.status_code == 202

    # 两次 202 各留一行(重复取消也是一次治理动作,审计面如实记两笔)。
    rows = await auth_service.list_audit(action="coverage.run.cancel")
    assert len(rows) == 2
    assert {row["details"]["run_id"] for row in rows} == {"cancel-1"}


async def test_cancel_finished_run_is_409(client, coverage_service):
    await _seed(coverage_service, "done-1", [_result(1, "q", "compiled")],
                created_at=_now())
    r = await client.post(
        "/v1/admin/question-coverage/runs/done-1/cancel")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "run_not_active"
