"""确认门:前置条件、批量、vouch/retire、预览脱敏、readiness 三态。"""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from trove.core.config import AgentConfig, MaskingConfig
from trove.services.evalset.models import EvalsetDoc, EvalsetError, EvalsetItem
from trove.services.evalset.service import EvalsetService
from trove.services.evalset.store import load_evalset, save_evalset
from trove.services.semantic_layer.models import SemanticModel


class Registry:
    def __init__(self, rows=None, fail=False):
        self.rows, self.fail = rows or [[1]], fail
        self.calls: list[tuple[str, str]] = []

    async def execute(self, sql, datasource):
        self.calls.append((sql, datasource))
        if self.fail:
            raise RuntimeError("bad sql")
        return SimpleNamespace(columns=["x"], rows=self.rows)

    def get(self, ds):
        return SimpleNamespace(dialect=lambda: "sqlite")


def svc(tmp_path, *, registry=None, audit=None, git=None,
        semantic_layer=None, config=None):
    kb = SimpleNamespace(
        kb_dir=tmp_path / ".trove" / "kb", git=git,
        semantics_path=lambda ds: tmp_path / ".trove" / "kb" / ds / "semantics.yml",
    )
    return EvalsetService(tmp_path, kb=kb, registry=registry or Registry(),
                          semantic_layer=semantic_layer, config=config,
                          audit=audit)


def model_provider(model):
    """语义层 provider 的最小 fake:``model()`` 同步返回给定值。"""
    return SimpleNamespace(model=lambda: model)


def seed(tmp_path, item):
    save_evalset(tmp_path, EvalsetDoc(datasource="demo", items=[item]))


async def test_confirm_requires_reference_sql(tmp_path):
    seed(tmp_path, EvalsetItem(qid="e-a", question="q", reference_sql="",
                               source="production", reference_origin="pipeline",
                               status="pending", needs_reference=True))
    with pytest.raises(EvalsetError) as e:
        await svc(tmp_path).confirm("demo", "e-a", actor="admin")
    assert e.value.code == "evalset.missing_reference"


async def test_confirm_rejects_shape_and_leakage(tmp_path):
    seed(tmp_path, EvalsetItem(qid="e-a", question="q", reference_sql="SELECT 1",
                               source="manual", reference_origin="human",
                               status="pending", match="shape"))
    with pytest.raises(EvalsetError) as e:
        await svc(tmp_path).confirm("demo", "e-a", actor="admin")
    assert e.value.code == "evalset.unsupported_match"

    kb_dir = tmp_path / ".trove" / "kb" / "demo"
    kb_dir.mkdir(parents=True)
    (kb_dir / "examples.yml").write_text(
        "examples:\n  - question: q\n    sql: SELECT 1\n", encoding="utf-8")
    seed(tmp_path, EvalsetItem(qid="e-b", question="q", reference_sql="SELECT 1",
                               source="manual", reference_origin="human",
                               status="pending"))
    with pytest.raises(EvalsetError) as e:
        await svc(tmp_path).confirm("demo", "e-b", actor="admin")
    assert e.value.code == "evalset.leakage"


async def test_confirm_success_sets_actor_and_commits(tmp_path):
    commits = []
    git = SimpleNamespace(commit_files=lambda *a, **k: commits.append((a, k)))
    audits = []
    async def audit(action, actor, details):
        audits.append((action, actor))
    seed(tmp_path, EvalsetItem(qid="e-a", question="q", reference_sql="SELECT 1",
                               source="manual", reference_origin="human",
                               status="pending"))
    out = await svc(tmp_path, git=git, audit=audit).confirm(
        "demo", "e-a", actor="admin")
    assert out["status"] == "confirmed" and out["confirmed_by"] == "admin"
    assert len(out["evalset_rev"]) == 16
    assert commits and audits == [("evalset.confirm", "admin")]


async def test_batch_partial_failure_is_isolated(tmp_path):
    save_evalset(tmp_path, EvalsetDoc(datasource="demo", items=[
        EvalsetItem(qid="e-ok", question="q1", reference_sql="SELECT 1",
                    source="manual", reference_origin="human", status="pending"),
        EvalsetItem(qid="e-bad", question="q2", reference_sql="",
                    source="manual", reference_origin="human", status="pending"),
    ]))
    out = await svc(tmp_path).confirm_batch(
        "demo", ["e-ok", "e-bad"], actor="admin")
    assert out["applied"] == 1 and out["failed"] == 1
    assert out["results"][1]["error"]["code"] == "evalset.missing_reference"
    docs, _, _ = load_evalset(tmp_path, "demo")
    assert {i.qid: i.status for i in docs.items} == {
        "e-ok": "confirmed", "e-bad": "pending"}


async def test_batch_over_limit_rejects_whole_batch(tmp_path):
    with pytest.raises(EvalsetError) as e:
        await svc(tmp_path).confirm_batch(
            "demo", [f"e-{i}" for i in range(201)], actor="admin")
    assert e.value.code == "evalset.invalid_batch"
    assert not (tmp_path / ".trove" / "evalsets" / "demo" / "evalset.yml").exists()


async def test_vouch_only_confirmed_semantic_and_keeps_source(tmp_path):
    seed(tmp_path, EvalsetItem(qid="e-a", question="q", reference_sql="SELECT 1",
                               source="semantic",
                               reference_origin="semantic_compiler",
                               status="pending"))
    with pytest.raises(EvalsetError):
        await svc(tmp_path).vouch("demo", "e-a", actor="admin", note="核验")
    doc, _, _ = load_evalset(tmp_path, "demo")
    doc.items[0].status = "confirmed"
    save_evalset(tmp_path, doc)
    out = await svc(tmp_path).vouch("demo", "e-a", actor="admin", note="核验")
    assert out["reference_origin"] == "human" and out["source"] == "semantic"
    assert out["vouched_by"] == "admin"


async def test_retire_only_confirmed_and_stops_scoring(tmp_path):
    seed(tmp_path, EvalsetItem(qid="e-a", question="q", reference_sql="SELECT 1",
                               source="manual", reference_origin="human",
                               status="pending"))
    with pytest.raises(EvalsetError):
        await svc(tmp_path).retire("demo", "e-a", actor="admin")
    doc, _, _ = load_evalset(tmp_path, "demo")
    doc.items[0].status = "confirmed"
    save_evalset(tmp_path, doc)
    out = await svc(tmp_path).retire("demo", "e-a", actor="admin", reason="口径变了")
    assert out["status"] == "retired"


async def test_edit_confirmed_requires_note_and_reconfirms(tmp_path):
    seed(tmp_path, EvalsetItem(qid="e-a", question="q", reference_sql="SELECT 1",
                               source="manual", reference_origin="human",
                               status="confirmed", confirmed_by="old"))
    with pytest.raises(EvalsetError) as e:
        await svc(tmp_path).edit("demo", "e-a", actor="new", note="")
    assert e.value.code == "evalset.note_required"
    out = await svc(tmp_path).edit(
        "demo", "e-a", actor="new", question="q2", note="改问法")
    assert out["confirmed_by"] == "new"
    assert "edited after confirm by new" in out["note"]


async def test_preview_refuses_when_masking_errors(tmp_path, monkeypatch):
    seed(tmp_path, EvalsetItem(qid="e-a", question="q", reference_sql="SELECT 1",
                               source="manual", reference_origin="human",
                               status="pending"))
    from trove.services.evalset import service as s

    def bad_masker(**kw):
        def apply(rows, columns, **k):
            from trove.services.authz.masking import MaskingError
            raise MaskingError("模型读不出")
        return apply

    monkeypatch.setattr(s, "build_masker", bad_masker)
    # 模型可读(过执行前门禁),失败发生在 apply —— 这条路径仍须是拒绝。
    provider = model_provider(SemanticModel())
    with pytest.raises(EvalsetError) as e:
        await svc(tmp_path, semantic_layer=provider).preview("demo", "e-a")
    assert e.value.code == "evalset.masking_refused"


async def test_readiness_three_states(tmp_path):
    service = svc(tmp_path)
    empty = await service.readiness("demo")
    assert empty["latest_runs"]["verified"] is None
    assert empty["available"] is True

    root = tmp_path / ".trove" / "eval" / "evalsets" / "demo" / "verified"
    root.mkdir(parents=True)
    (root / "latest.summary.json").write_text(json.dumps({"n": 0, "ex": 0.0}),
                                             encoding="utf-8")
    zero = await service.readiness("demo")
    assert zero["latest_runs"]["verified"]["n"] == 0

    (root / "latest.summary.json").write_text("{bad", encoding="utf-8")
    broken = await service.readiness("demo")
    assert broken["available"] is False
    assert any(d["code"] == "run_store_unreadable" for d in broken["degraded"])


# ── C1:脱敏开启但没有可读语义模型 → 取数之前拒绝 ─────────────────


@pytest.mark.parametrize("kind", ["builtin", "no_model", "raises"])
async def test_preview_refuses_masking_without_model_before_query(tmp_path, kind):
    """masking 默认开启;模型不可读时预览必须拒绝且一行都不取。

    不经 monkeypatch ``build_masker``:这里走的是真实构造点 —— 恰好是
    C1 漏掉的那条缝(有 masker ≠ 脱敏发生过)。
    """
    registry = Registry()
    seed(tmp_path, EvalsetItem(qid="e-a", question="q", reference_sql="SELECT 1",
                               source="manual", reference_origin="human",
                               status="pending"))
    kwargs = {}
    if kind == "no_model":
        kwargs["semantic_layer"] = model_provider(None)
    elif kind == "raises":
        def boom():
            raise RuntimeError("provider exploded")
        kwargs["semantic_layer"] = SimpleNamespace(model=boom)

    with pytest.raises(EvalsetError) as e:
        await svc(tmp_path, registry=registry, **kwargs).preview("demo", "e-a")
    assert e.value.code == "evalset.masking_refused"
    assert registry.calls == []


async def test_preview_returns_rows_when_masking_disabled(tmp_path):
    registry = Registry(rows=[[7]])
    config = AgentConfig(masking=MaskingConfig(enabled=False))
    seed(tmp_path, EvalsetItem(qid="e-a", question="q", reference_sql="SELECT 1",
                               source="manual", reference_origin="human",
                               status="pending"))
    out = await svc(tmp_path, registry=registry, config=config).preview(
        "demo", "e-a")
    assert out["masking"] == "off"
    assert out["rows"] == [[7]]
    assert out["columns"] == ["x"]


async def test_preview_with_working_masker_returns_report(tmp_path):
    registry = Registry(rows=[[1]])
    provider = model_provider(SemanticModel())
    seed(tmp_path, EvalsetItem(qid="e-a", question="q", reference_sql="SELECT 1",
                               source="manual", reference_origin="human",
                               status="pending"))
    out = await svc(tmp_path, registry=registry,
                    semantic_layer=provider).preview("demo", "e-a")
    assert out["masking"] == {"fields": {}, "bypass": False}
    assert out["rows"] == [[1]]


# ── I2/M7/M8:edit 清除 needs_reference、确认清泄漏标记、审计按值比较 ──


async def test_edit_reference_sql_clears_needs_reference(tmp_path):
    seed(tmp_path, EvalsetItem(qid="e-a", question="q", reference_sql="SELECT 1",
                               source="production", reference_origin="pipeline",
                               status="pending", needs_reference=True))
    service = svc(tmp_path)
    out = await service.edit("demo", "e-a", actor="admin",
                             reference_sql="SELECT 2")
    assert out["reference_sql"] == "SELECT 2"
    assert out["needs_reference"] is False
    confirmed = await service.confirm("demo", "e-a", actor="admin")
    assert confirmed["status"] == "confirmed"


async def test_edit_validates_explicit_sql_even_when_unchanged(tmp_path):
    seed(tmp_path, EvalsetItem(qid="e-a", question="q",
                               reference_sql="SELEC 1",
                               source="production", reference_origin="pipeline",
                               status="pending", needs_reference=True))
    with pytest.raises(EvalsetError) as e:
        await svc(tmp_path).edit("demo", "e-a", actor="admin",
                                 reference_sql="SELEC 1")
    assert e.value.code == "evalset.sql_invalid"


async def test_confirm_clears_stale_leakage_marker(tmp_path):
    seed(tmp_path, EvalsetItem(qid="e-a", question="q", reference_sql="SELECT 1",
                               source="manual", reference_origin="human",
                               status="pending", leakage=True,
                               leakage_hit="word: old question"))
    out = await svc(tmp_path).confirm("demo", "e-a", actor="admin")
    assert out["leakage"] is False
    assert out["leakage_hit"] == ""


async def test_edit_audit_flags_compare_values(tmp_path):
    audits = []

    async def audit(action, actor, details):
        audits.append(details)

    seed(tmp_path, EvalsetItem(qid="e-a", question="q", reference_sql="SELECT 1",
                               source="manual", reference_origin="human",
                               status="pending"))
    service = svc(tmp_path, audit=audit)
    await service.edit("demo", "e-a", actor="admin",
                       question="q", reference_sql="SELECT 1")
    assert audits[-1]["question_changed"] is False
    assert audits[-1]["reference_sql_changed"] is False

    await service.edit("demo", "e-a", actor="admin",
                       question="q2", reference_sql="SELECT 2")
    assert audits[-1]["question_changed"] is True
    assert audits[-1]["reference_sql_changed"] is True


# ── M5:commit_files 状态 + trailers ────────────────────────────────


async def test_commit_passes_trailers_and_logs_status_failure(tmp_path, caplog):
    commits = []
    git = SimpleNamespace(
        commit_files=lambda *a, **k: commits.append((a, k)) or {
            "committed": False, "reason": "lint-failed",
        })
    seed(tmp_path, EvalsetItem(qid="e-a", question="q", reference_sql="SELECT 1",
                               source="manual", reference_origin="human",
                               status="pending"))
    with caplog.at_level(logging.WARNING):
        out = await svc(tmp_path, git=git).confirm("demo", "e-a", actor="admin")
    # 版本化失败绝不阻断已经成功的写入。
    assert out["status"] == "confirmed"
    assert commits[0][1]["trailers"] == {
        "Approved-by": "admin", "Generator": "evalset"}
    assert "lint-failed" in caplog.text


# ── I3:资产读不出 ≠ 空集 ──────────────────────────────────────────


async def test_readiness_unreadable_asset_is_not_an_empty_set(tmp_path):
    path = tmp_path / ".trove" / "evalsets" / "demo" / "evalset.yml"
    path.parent.mkdir(parents=True)
    path.write_text("{bad", encoding="utf-8")

    out = await svc(tmp_path).readiness("demo")
    assert out["available"] is False
    codes = [entry["code"] for entry in out["degraded"]]
    assert codes.count("asset_unreadable") == 1
    # 计数/覆盖/修订都无从谈起:null(不知道),不是全 0 的「健康空集」。
    assert out["counts"] is None
    assert out["by_tier"] is None
    assert out["coverage"] is None
    assert out["evalset_rev"] is None


# ── I4:create/reject/list_items 回归 ───────────────────────────────


async def test_create_lands_pending_manual_verified(tmp_path):
    audits = []
    commits = []

    async def audit(action, actor, details):
        audits.append((action, actor))

    git = SimpleNamespace(
        commit_files=lambda *a, **k: commits.append((a, k)) or {
            "committed": True,
        })
    out = await svc(tmp_path, git=git, audit=audit).create(
        "demo", question="How many loans?", reference_sql="SELECT 1",
        tags={"metric": "loans"}, actor="admin")
    assert out["status"] == "pending"
    assert out["source"] == "manual"
    assert out["reference_origin"] == "human"
    assert out["match"] == "exact"
    assert out["tier"] == "verified"
    assert len(out["evalset_rev"]) == 16
    assert out["created_by"] == "admin" and out["generator"] == "manual"
    assert audits == [("evalset.create", "admin")]
    assert commits[0][1]["trailers"] == {
        "Approved-by": "admin", "Generator": "evalset"}

    doc, issues, exists = load_evalset(tmp_path, "demo")
    assert exists and not issues
    assert [(item.qid, item.status) for item in doc.items] == [
        (out["qid"], "pending")]


async def test_create_guard_failures_leave_no_asset(tmp_path):
    path = tmp_path / ".trove" / "evalsets" / "demo" / "evalset.yml"
    kb_dir = tmp_path / ".trove" / "kb" / "demo"
    kb_dir.mkdir(parents=True)
    (kb_dir / "examples.yml").write_text(
        "examples:\n  - question: leaked question\n    sql: SELECT 1\n",
        encoding="utf-8")
    service = svc(tmp_path)

    # 泄漏不受 force 豁免,且不落盘。
    with pytest.raises(EvalsetError) as e:
        await service.create("demo", question="leaked question",
                             reference_sql="SELECT 2", actor="admin")
    assert e.value.code == "evalset.leakage"
    assert not path.exists()

    # 语法护栏失败同样不落盘。
    with pytest.raises(EvalsetError) as e:
        await service.create("demo", question="fresh question",
                             reference_sql="SELEC 1", actor="admin")
    assert e.value.code == "evalset.sql_invalid"
    assert not path.exists()


async def test_create_duplicate_paths(tmp_path):
    service = svc(tmp_path)
    first = await service.create(
        "demo", question="How many loans?", reference_sql="SELECT 1",
        actor="admin")

    with pytest.raises(EvalsetError) as e:
        await service.create("demo", question="How many loans?",
                             reference_sql="SELECT 1", actor="admin")
    assert e.value.code == "evalset.duplicate_question"
    assert e.value.issues[0]["qid"] == first["qid"]

    with pytest.raises(EvalsetError) as e:
        await service.create("demo", question="How many loans?",
                             reference_sql="SELECT 2", actor="admin")
    assert e.value.code == "evalset.duplicate_conflict"

    # 两次被拒的写入都没有改动资产。
    unchanged, _, _ = load_evalset(tmp_path, "demo")
    assert [item.qid for item in unchanged.items] == [first["qid"]]

    forced = await service.create(
        "demo", question="How many loans?", reference_sql="SELECT 2",
        actor="admin", force=True)
    assert forced["duplicate_of"] == first["qid"]
    assert forced["status"] == "pending"
    doc, _, _ = load_evalset(tmp_path, "demo")
    assert len(doc.items) == 2


async def test_reject_tombstones_and_resolves_duplicates(tmp_path):
    save_evalset(tmp_path, EvalsetDoc(datasource="demo", items=[
        EvalsetItem(qid="e-a", question="q", reference_sql="SELECT 1",
                    source="manual", reference_origin="human",
                    status="pending"),
        EvalsetItem(qid="e-b", question="other q", reference_sql="SELECT 2",
                    source="manual", reference_origin="human",
                    status="pending", duplicate_of="e-a"),
    ]))
    out = await svc(tmp_path).reject("demo", "e-a", actor="admin", reason="重复")
    assert out["status"] == "rejected"
    assert out["rejection_reason"] == "重复"

    doc, _, _ = load_evalset(tmp_path, "demo")
    follower = next(item for item in doc.items if item.qid == "e-b")
    assert follower.duplicate_of == ""
    assert "duplicate resolved: e-a rejected" in follower.note


async def test_list_items_ordering_filters_and_counts(tmp_path):
    save_evalset(tmp_path, EvalsetDoc(datasource="demo", items=[
        EvalsetItem(qid="e-1", question="Alpha loans", reference_sql="SELECT 1",
                    source="manual", reference_origin="human",
                    status="confirmed", created_at="2026-01-01T00:00:00+00:00"),
        EvalsetItem(qid="e-2", question="beta loans", reference_sql="SELECT 2",
                    source="semantic",
                    reference_origin="semantic_compiler", status="pending",
                    created_at="2026-01-03T00:00:00+00:00"),
        EvalsetItem(qid="e-3", question="gamma", reference_sql="SELECT 3",
                    source="synthetic", reference_origin="llm",
                    status="pending", created_at="2026-01-02T00:00:00+00:00"),
        EvalsetItem(qid="e-4", question="delta", reference_sql="SELECT 4",
                    source="production", reference_origin="pipeline",
                    status="rejected", created_at="2026-01-04T00:00:00+00:00"),
        EvalsetItem(qid="e-5", question="epsilon", reference_sql="SELECT 5",
                    source="manual", reference_origin="human",
                    status="retired", created_at="2026-01-05T00:00:00+00:00"),
    ]))
    service = svc(tmp_path)

    listed = await service.list_items("demo")
    # 状态组顺序 pending → confirmed → rejected → retired;
    # 组内 created_at 降序(pending:e-2 新于 e-3)。
    assert [item["qid"] for item in listed["items"]] == [
        "e-2", "e-3", "e-1", "e-4", "e-5"]
    assert listed["counts"] == {
        "pending": 2, "confirmed": 1, "rejected": 1, "retired": 1, "total": 5}

    assert [i["qid"] for i in (await service.list_items(
        "demo", status="pending"))["items"]] == ["e-2", "e-3"]
    assert [i["qid"] for i in (await service.list_items(
        "demo", source="manual"))["items"]] == ["e-1", "e-5"]
    # verified = source ∈ {production, manual} 或人工参考 SQL。
    assert [i["qid"] for i in (await service.list_items(
        "demo", tier="verified"))["items"]] == ["e-1", "e-4", "e-5"]
    assert [i["qid"] for i in (await service.list_items(
        "demo", q="LOANS"))["items"]] == ["e-2", "e-1"]
    # 计数是全量投影,不随过滤器变化。
    filtered = await service.list_items("demo", status="pending")
    assert filtered["counts"]["total"] == 5
    assert all(item["tier"] for item in filtered["items"])
