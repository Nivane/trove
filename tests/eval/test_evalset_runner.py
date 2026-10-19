"""runner:判定映射、GOLD_ERROR 分母、成本护栏、KB 快照、tier 分文件。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from trove.agent.session import SessionManager
from trove.eval.evalset_run import (
    RunOptions, RunStoreUnreadable, read_latest_entries, read_latest_summary,
    run_evalset,
)
from trove.eval.graph_runner import run_question as graph_run_question
from trove.services.evalset.models import EvalsetDoc, EvalsetItem
from trove.services.evalset.store import save_evalset
from trove.workflow.state import WorkflowState


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


def run_state(*, sql="", error="", refusal=None, compiled=False,
              compile_partial=False, rows=None, **overrides):
    """runner 替身的最终状态(判定优先级测试用)。"""
    base = dict(
        sql=sql, error=error, refusal=refusal, compile_partial=compile_partial,
        compiled=compiled, compile_meta={}, kb_hits=[], retry_count=0,
        consensus=None, confidence=0.0, selection={}, fix_mode="",
        rollback_target="", validation_hits=[], candidates=[], rows=rows,
        plan={}, matched_tables=[], evidence="",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


async def test_match_and_mismatch(tmp_path, monkeypatch):
    seed(tmp_path, [confirmed("e-1", "SELECT 1"), confirmed("e-2", "SELECT 2")])
    reg = Registry({"SELECT 1": [[1]], "SELECT 2": [[2]]})
    async def fake_run(graph, question, **kw):
        return await graph_returning("SELECT 1")(graph, question, **kw)
    monkeypatch.setattr("trove.eval.evalset_run.run_question", fake_run)
    out = await run_evalset(options(tmp_path, object(), reg))
    # 预测恒为 SELECT 1 → 行 [[1]];参考 e-2 的行是 [[2]] → MISMATCH
    assert [e["verdict"] for e in out.entries] == ["MATCH", "MISMATCH"]


async def test_both_empty_is_match(tmp_path, monkeypatch):
    seed(tmp_path, [confirmed("e-1", "SELECT 1")])
    reg = Registry({"SELECT 1": []})
    async def fake_run(graph, question, **kw):
        return await graph_returning("SELECT 1")(graph, question, **kw)
    monkeypatch.setattr("trove.eval.evalset_run.run_question", fake_run)
    out = await run_evalset(options(tmp_path, object(), reg))
    assert out.entries[0]["verdict"] == "MATCH"


async def test_only_one_empty_is_mismatch(tmp_path, monkeypatch):
    seed(tmp_path, [confirmed("e-1", "SELECT 1")])
    reg = Registry({"SELECT 1": [[1]], "SELECT 0": []})  # 参考有行
    async def fake_run(graph, question, **kw):
        return await graph_returning("SELECT 0")(graph, question, **kw)  # 预测空
    monkeypatch.setattr("trove.eval.evalset_run.run_question", fake_run)
    out = await run_evalset(options(tmp_path, object(), reg))
    assert out.entries[0]["verdict"] == "MISMATCH"


async def test_gold_error_excluded_from_denominator(tmp_path, monkeypatch):
    seed(tmp_path, [confirmed("e-1", "SELECT bad")])
    reg = Registry(fail_sql={"SELECT bad"})
    async def fake_run(graph, question, **kw):
        return await graph_returning("SELECT 1")(graph, question, **kw)
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
    # R23:没有可运行条目 → 响亮 exit 2;空 latest.* 会覆盖上一份好结果,不写。
    assert out.exit_code == 2 and out.entries == []
    assert out.skipped == 1 and out.summary["skipped"] == 1
    assert not (tmp_path / ".trove/eval/evalsets/demo/verified/latest.jsonl").exists()


async def test_kb_rev_changed_mid_run(tmp_path, monkeypatch):
    seed(tmp_path, [confirmed("e-1", "SELECT 1")])
    kb = KB(tmp_path / ".trove" / "kb", "r1")
    async def fake_run(graph, question, **kw):
        kb.rev = "r2"
        return await graph_returning("SELECT 1")(graph, question, **kw)
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
        return await graph_returning("SELECT 1")(graph, question, **kw)
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


async def test_formal_without_registry_exits_2_zero_llm(tmp_path, monkeypatch):
    """R21:参考 SQL 无法执行 → 没有题可判;绝不烧 LLM 跑一轮全 GOLD_ERROR。"""
    seed(tmp_path, [confirmed("e-1", "SELECT 1")])
    called = []
    async def fake_run(*a, **k):
        called.append(1)
    monkeypatch.setattr("trove.eval.evalset_run.run_question", fake_run)
    out = await run_evalset(options(tmp_path, object(), None, yes=True))
    assert out.exit_code == 2 and called == []
    assert out.message
    assert not (tmp_path / ".trove/eval/evalsets/demo/verified/latest.jsonl").exists()


async def test_error_wins_over_refusal(tmp_path, monkeypatch):
    """判定顺序:final.error 先于 refusal(有错就是生成失败,不是拒绝)。"""
    seed(tmp_path, [confirmed("e-1", "SELECT 1")])
    async def fake_run(graph, question, **kw):
        return run_state(error="generation retries exhausted",
                         refusal={"reason": "coverage"})
    monkeypatch.setattr("trove.eval.evalset_run.run_question", fake_run)
    out = await run_evalset(options(tmp_path, object(), Registry({"SELECT 1": [[1]]})))
    assert out.entries[0]["verdict"] == "GENERATION_ERROR"
    assert out.entries[0]["path"] == "refused"


async def test_empty_sql_verdict(tmp_path, monkeypatch):
    """无 error、无 refusal、无 SQL → EMPTY_SQL(不是 EXECUTION_ERROR)。"""
    seed(tmp_path, [confirmed("e-1", "SELECT 1")])
    async def fake_run(graph, question, **kw):
        return run_state()
    monkeypatch.setattr("trove.eval.evalset_run.run_question", fake_run)
    out = await run_evalset(options(tmp_path, object(), Registry({"SELECT 1": [[1]]})))
    assert out.entries[0]["verdict"] == "EMPTY_SQL"


async def test_kb_rev_end_unavailable_is_degraded_not_changed(tmp_path, monkeypatch):
    """收尾快照读失败 ≠ KB 变了:标 kb_rev_end_unavailable,不谎报变更。"""
    seed(tmp_path, [confirmed("e-1", "SELECT 1")])
    class FlakyKB(KB):
        def __init__(self, kb_dir):
            super().__init__(kb_dir)
            self.calls = 0
        async def kb_rev(self, ds):
            self.calls += 1
            if self.calls > 1:
                raise RuntimeError("rev read failed")
            return {"rev": "r1", "files": {}}
    kb = FlakyKB(tmp_path / ".trove" / "kb")
    async def fake_run(graph, question, **kw):
        return run_state(sql="SELECT 1", compiled=True, rows=[[1]])
    monkeypatch.setattr("trove.eval.evalset_run.run_question", fake_run)
    out = await run_evalset(options(
        tmp_path, object(), Registry({"SELECT 1": [[1]]}), kb=kb))
    assert out.summary["kb_rev_changed_mid_run"] is False
    assert out.summary["kb_rev_end"] is None
    assert [d["code"] for d in out.summary["degraded"]] == ["kb_rev_end_unavailable"]


async def test_graph_runner_merges_updates_and_traces(monkeypatch):
    """graph_runner 冒烟:updates 合并成合法 WorkflowState,trace 事件照发。"""
    class FakeGraph:
        def __init__(self, updates):
            self.updates = updates
        async def astream(self, state, config=None, stream_mode=None):
            for update in self.updates:
                yield update

    events = []
    monkeypatch.setattr(
        SessionManager, "_trace_run_start",
        staticmethod(lambda state: events.append(("run", state.run_id))))
    monkeypatch.setattr(
        SessionManager, "_trace_step",
        staticmethod(lambda state, step: events.append(("step", step["node"]))))
    monkeypatch.setattr(
        SessionManager, "_trace_run_finish",
        staticmethod(lambda run_id, final, *a, **k: events.append(
            ("finish", run_id, final.sql))))

    graph = FakeGraph([
        {"schema_linking": {"matched_tables": ["t1"]}},
        {"gen_generate": {"sql": "SELECT 1"}},
    ])
    final = await graph_run_question(
        graph, "q1", run_id="r-smoke", index=3, datasource="demo")
    assert isinstance(final, WorkflowState)
    assert final.session_id == "evalset-r-smoke-3"
    assert final.question == "q1" and final.datasource == "demo"
    assert final.matched_tables == ["t1"] and final.sql == "SELECT 1"
    assert final.principal is not None  # authz 缺主体即拒绝,runner 必须注入
    assert events == [
        ("run", "r-smoke"), ("step", "schema_linking"),
        ("step", "gen_generate"), ("finish", "r-smoke", "SELECT 1"),
    ]
