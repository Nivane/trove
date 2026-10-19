"""runner:判定映射、GOLD_ERROR 分母、成本护栏、KB 快照、tier 分文件。"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from trove.eval.evalset_run import (
    RunOptions, RunStoreUnreadable, read_latest_entries, read_latest_summary,
    run_evalset,
)
from trove.services.evalset.models import EvalsetDoc, EvalsetItem
from trove.services.evalset.store import save_evalset


class Registry:
    def __init__(self, rows_by_sql=None, fail_sql=()):
        self.rows_by_sql = rows_by_sql or {}
        self.fail_sql = set(fail_sql)

    async def execute(self, sql, datasource):
        if sql in self.fail_sql:
            raise RuntimeError("bad sql")
        return SimpleNamespace(columns=["x"], rows=self.rows_by_sql.get(sql, []))


def seed(tmp_path, items):
    save_evalset(tmp_path, EvalsetDoc(datasource="demo", items=items))


def confirmed(qid, sql, *, match="exact", tier="verified"):
    source = "manual" if tier == "verified" else "semantic"
    origin = "human" if tier == "verified" else "semantic_compiler"
    return EvalsetItem(qid=qid, question=f"question {qid}", reference_sql=sql,
                       source=source, reference_origin=origin,
                       status="confirmed", match=match, tags={})


class KB:
    def __init__(self, kb_dir, rev="r1"):
        self.kb_dir = kb_dir
        self.rev = rev

    async def ensure_synced(self, ds):
        return None

    async def kb_rev(self, ds):
        if self.rev is None:
            return None
        return {"rev": self.rev, "files": {}}


def options(tmp_path, graph, registry, *, yes=True, kb=None, **kw):
    base = dict(project_root=tmp_path, datasource="demo", tier="verified",
                yes=yes, kb=kb or KB(tmp_path / ".trove" / "kb"), registry=registry,
                config=object(), graph=graph)
    base.update(kw)
    return RunOptions(**base)


def graph_returning(sql):
    async def run_question(graph, question, **kw):
        return SimpleNamespace(
            sql=sql, error="", refusal=None, compile_partial=False,
            compiled=True, compile_meta={"outcome": "compiled"},
            kb_hits=[], retry_count=0, consensus=None, confidence=0.0,
            selection={}, fix_mode="", rollback_target="",
            validation_hits=[], candidates=[], rows=[[999]],
            plan={}, matched_tables=[], evidence="", run_id=kw.get("run_id", ""),
        )
    return run_question


async def test_match_and_mismatch(tmp_path, monkeypatch):
    seed(tmp_path, [confirmed("e-1", "SELECT 1"), confirmed("e-2", "SELECT 2")])
    reg = Registry({"SELECT 1": [[1]], "SELECT 2": [[2]]})
    async def fake_run(graph, question, **kw):
        return graph_returning("SELECT 1")(graph, question, **kw)
    monkeypatch.setattr("trove.eval.evalset_run.run_question", fake_run)
    out = await run_evalset(options(tmp_path, object(), reg))
    # 预测恒为 SELECT 1 → 行 [[1]];参考 e-2 的行是 [[2]] → MISMATCH
    assert [e["verdict"] for e in out.entries] == ["MATCH", "MISMATCH"]


async def test_both_empty_is_match(tmp_path, monkeypatch):
    seed(tmp_path, [confirmed("e-1", "SELECT 1")])
    reg = Registry({"SELECT 1": []})
    async def fake_run(graph, question, **kw):
        return graph_returning("SELECT 1")(graph, question, **kw)
    monkeypatch.setattr("trove.eval.evalset_run.run_question", fake_run)
    out = await run_evalset(options(tmp_path, object(), reg))
    assert out.entries[0]["verdict"] == "MATCH"


async def test_only_one_empty_is_mismatch(tmp_path, monkeypatch):
    seed(tmp_path, [confirmed("e-1", "SELECT 1")])
    reg = Registry({"SELECT 1": [[1]], "SELECT 0": []})  # 参考有行
    async def fake_run(graph, question, **kw):
        return graph_returning("SELECT 0")(graph, question, **kw)  # 预测空
    monkeypatch.setattr("trove.eval.evalset_run.run_question", fake_run)
    out = await run_evalset(options(tmp_path, object(), reg))
    assert out.entries[0]["verdict"] == "MISMATCH"


async def test_gold_error_excluded_from_denominator(tmp_path, monkeypatch):
    seed(tmp_path, [confirmed("e-1", "SELECT bad")])
    reg = Registry(fail_sql={"SELECT bad"})
    async def fake_run(graph, question, **kw):
        return graph_returning("SELECT 1")(graph, question, **kw)
    monkeypatch.setattr("trove.eval.evalset_run.run_question", fake_run)
    out = await run_evalset(options(tmp_path, object(), reg))
    assert out.entries[0]["verdict"] == "GOLD_ERROR"
    assert out.summary["ex_judged"] == 0
    assert out.summary["reference_errors"] == 1


async def test_no_yes_is_dry_run_zero_llm(tmp_path, monkeypatch):
    seed(tmp_path, [confirmed("e-1", "SELECT 1")])
    called = []
    async def fake_run(*a, **k):
        called.append(1)
    monkeypatch.setattr("trove.eval.evalset_run.run_question", fake_run)
    out = await run_evalset(options(tmp_path, object(), Registry(), yes=False))
    assert out.exit_code == 0 and called == []
    assert out.message


async def test_compile_only_zero_llm(tmp_path, monkeypatch):
    seed(tmp_path, [confirmed("e-1", "SELECT 1")])
    called = []
    async def fake_run(*a, **k):
        called.append(1)
    monkeypatch.setattr("trove.eval.evalset_run.run_question", fake_run)
    out = await run_evalset(options(tmp_path, object(), Registry(), yes=False,
                                    compile_only=True))
    assert out.exit_code == 0 and called == []


async def test_shape_is_skipped_not_in_denominator(tmp_path, monkeypatch):
    seed(tmp_path, [confirmed("e-1", "SELECT 1", match="shape")])
    async def fake_run(*a, **k):
        raise AssertionError("must not run")
    monkeypatch.setattr("trove.eval.evalset_run.run_question", fake_run)
    out = await run_evalset(options(tmp_path, object(), Registry()))
    assert out.skipped == 1 and out.entries == []
    assert out.summary["skipped"] == 1


async def test_kb_rev_changed_mid_run(tmp_path, monkeypatch):
    seed(tmp_path, [confirmed("e-1", "SELECT 1")])
    kb = KB(tmp_path / ".trove" / "kb", "r1")
    async def fake_run(graph, question, **kw):
        kb.rev = "r2"
        return graph_returning("SELECT 1")(graph, question, **kw)
    monkeypatch.setattr("trove.eval.evalset_run.run_question", fake_run)
    out = await run_evalset(options(tmp_path, object(), Registry({"SELECT 1": [[1]]}),
                                    kb=kb))
    assert out.summary["kb_rev_changed_mid_run"] is True
    assert any(d["code"] == "kb_changed_mid_run" for d in out.summary["degraded"])


async def test_busy_kb_lock_exits_2_without_run(tmp_path, monkeypatch):
    from trove.services.kb.locks import KbInitLock
    seed(tmp_path, [confirmed("e-1", "SELECT 1")])
    called = []
    async def fake_run(*a, **k):
        called.append(1)
    monkeypatch.setattr("trove.eval.evalset_run.run_question", fake_run)
    kb = KB(tmp_path / ".trove" / "kb")
    lock = KbInitLock(kb.kb_dir / ".locks")
    with lock.acquire("demo"):
        out = await run_evalset(options(tmp_path, object(), Registry(), kb=kb))
    assert out.exit_code == 2 and called == []


async def test_tiers_write_separate_files(tmp_path, monkeypatch):
    seed(tmp_path, [confirmed("e-1", "SELECT 1", tier="verified"),
                    confirmed("e-2", "SELECT 2", tier="semantic")])
    async def fake_run(graph, question, **kw):
        return graph_returning("SELECT 1")(graph, question, **kw)
    monkeypatch.setattr("trove.eval.evalset_run.run_question", fake_run)
    reg = Registry({"SELECT 1": [[1]], "SELECT 2": [[1]]})
    v = await run_evalset(options(tmp_path, object(), reg, tier="verified"))
    s = await run_evalset(options(tmp_path, object(), reg, tier="semantic"))
    assert (tmp_path / ".trove/eval/evalsets/demo/verified/latest.jsonl").exists()
    assert (tmp_path / ".trove/eval/evalsets/demo/semantic/latest.jsonl").exists()
    assert v.entries[0]["qid"] == "e-1" and s.entries[0]["qid"] == "e-2"


async def test_read_latest_missing_vs_unreadable(tmp_path):
    assert read_latest_summary(tmp_path, "demo", "verified") is None
    assert read_latest_entries(tmp_path, "demo", "verified") is None
    root = tmp_path / ".trove/eval/evalsets/demo/verified"
    root.mkdir(parents=True)
    (root / "latest.summary.json").write_text("{bad", encoding="utf-8")
    with pytest.raises(RunStoreUnreadable):
        read_latest_summary(tmp_path, "demo", "verified")
