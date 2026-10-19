"""确认门:前置条件、批量、vouch/retire、预览脱敏、readiness 三态。"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from trove.services.evalset.models import EvalsetDoc, EvalsetError, EvalsetItem
from trove.services.evalset.service import EvalsetService
from trove.services.evalset.store import load_evalset, save_evalset


class Registry:
    def __init__(self, rows=None, fail=False):
        self.rows, self.fail = rows or [[1]], fail

    async def execute(self, sql, datasource):
        if self.fail:
            raise RuntimeError("bad sql")
        return SimpleNamespace(columns=["x"], rows=self.rows)

    def get(self, ds):
        return SimpleNamespace(dialect=lambda: "sqlite")


def svc(tmp_path, *, registry=None, audit=None, git=None):
    kb = SimpleNamespace(
        kb_dir=tmp_path / ".trove" / "kb", git=git,
        semantics_path=lambda ds: tmp_path / ".trove" / "kb" / ds / "semantics.yml",
    )
    return EvalsetService(tmp_path, kb=kb, registry=registry or Registry(),
                          config=None, audit=audit)


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
    with pytest.raises(EvalsetError) as e:
        await svc(tmp_path).preview("demo", "e-a")
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
