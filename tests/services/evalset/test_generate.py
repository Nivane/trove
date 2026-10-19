"""semantic 生成:枚举确定性、编译 MISS 跳过、模板含自然名。"""
from __future__ import annotations

from trove.services.evalset.generate import (
    Candidate, GenerateContext, build_question, enumerate_candidates,
    generate_semantic,
)
from trove.services.semantic_layer.models import (
    SemanticDataset, SemanticField, SemanticMetric, SemanticModel,
)


def model():
    return SemanticModel(name="demo", datasets=[
        SemanticDataset(name="loan", fields=[
            SemanticField(name="district", expression="loan.district",
                          semantic_role="dimension", label="地区"),
            SemanticField(name="status", expression="loan.status",
                          semantic_role="enum", label="状态",
                          values=["A", "B", "C"]),
            SemanticField(name="date", expression="loan.date", is_time=True),
        ]),
    ], metrics=[SemanticMetric(
        name="loan_amount", expression="SUM(loan.amount)",
        datasets=["loan"], agg_time_dimension="loan.date",
        synonyms=["贷款总额"],
    )])


def ctx(**kw):
    base = dict(datasource="demo", lang="zh", dialect="sqlite", llm=None,
                registry=None, model=model())
    base.update(kw)
    return GenerateContext(**base)


def test_enumeration_is_stable_and_covers_all_kinds():
    a = enumerate_candidates(model(), limit=100)
    b = enumerate_candidates(model(), limit=100)
    assert [c.key() for c in a] == [c.key() for c in b]
    assert {c.kind for c in a} >= {
        "metric_dim", "metric_grain", "metric_dim_grain",
        "metric_filter", "metric_top_n",
    }
    assert any(c.value == "A" for c in a)
    # 截断取前 N 仍然是同一前缀
    assert [c.key() for c in enumerate_candidates(model(), limit=3)] == \
        [c.key() for c in a[:3]]


def test_question_contains_natural_names():
    cands = enumerate_candidates(model(), limit=100)
    dim = next(c for c in cands if c.kind == "metric_dim")
    q = build_question(dim, model(), lang="zh")
    assert "地区" in q and "贷款总额" in q


async def test_compile_miss_is_skipped(monkeypatch):
    from trove.services.semantic_layer import query as qmod

    calls = {"n": 0}

    def fake_compile(model, query, dialect="sqlite", matched=None):
        calls["n"] += 1
        if query.filters:
            raise qmod.SemanticQueryError("undeclared literal")
        return {"sql": "SELECT 1", "columns": ["x"], "datasets": ["loan"],
                "version": 1}

    monkeypatch.setattr("trove.services.evalset.generate.build_and_compile", fake_compile)
    result = await generate_semantic(ctx(), limit=100)
    assert result.status == "ok"
    assert result.skipped > 0
    assert all("SELECT 1" == it.reference_sql for it in result.items)
    assert all(it.source == "semantic" for it in result.items)
    assert all(it.reference_origin == "semantic_compiler" for it in result.items)


async def test_trial_failure_is_skipped(monkeypatch):
    from trove.services.evalset import generate as g

    monkeypatch.setattr(g, "build_and_compile",
                        lambda *a, **k: {"sql": "SELECT bad", "columns": [],
                                         "datasets": [], "version": 1})

    async def fake_trial(registry, ds, sql):
        return False

    monkeypatch.setattr(g, "trial_ok", fake_trial)
    result = await generate_semantic(ctx(datasource=None), limit=5)
    assert result.items == [] and result.skipped == 5


async def test_no_semantic_model():
    result = await generate_semantic(ctx(model=None), limit=5)
    assert result.status == "no_semantic_model"
    assert result.items == []


async def test_rewrite_failure_falls_back_to_templates(monkeypatch):
    class BadLLM:
        async def chat(self, **kw):
            return "not json"

    monkeypatch.setattr("trove.services.evalset.generate.build_and_compile",
                        lambda *a, **k: {"sql": "SELECT 1", "columns": [],
                                         "datasets": [], "version": 1})
    async def ok_trial(registry, ds, sql):
        return True
    monkeypatch.setattr("trove.services.evalset.generate.trial_ok", ok_trial)
    result = await generate_semantic(
        ctx(llm=BadLLM(), draft_model="mock/model"), limit=2, rewrite=True)
    assert result.items  # 模板仍产出
    assert any(d["code"] == "rewrite_failed" for d in result.degraded)


# ── 追加:构造契约(qualify / top_n / grain / filter / tags)────────────


async def test_generate_builds_qualified_queries_and_tags(monkeypatch):
    captured = []

    def fake_compile(model, query, dialect="sqlite", matched=None):
        captured.append(query)
        return {"sql": "SELECT 1", "columns": [], "datasets": ["loan"],
                "version": 1}

    monkeypatch.setattr("trove.services.evalset.generate.build_and_compile",
                        fake_compile)
    result = await generate_semantic(ctx(), limit=100)
    assert result.items

    # 维度一律用 dataset.field(qualified),tags 存裸字段名。
    dim_query = next(q for q in captured if q.dimensions)
    assert dim_query.dimensions == ["loan.district"]
    dim_item = next(it for it in result.items
                    if it.tags.get("dimension") == ["district"])
    assert dim_item.tags["metric"] == ["loan_amount"]
    assert "group_by" in dim_item.tags["pattern"]

    # grain 候选带 time_grain,且 field 已规范成 dataset.field。
    grain_query = next(q for q in captured if q.time_grain)
    assert grain_query.time_grain["field"] == "loan.date"
    assert grain_query.time_grain["grain"] in {"month", "week", "day"}
    assert any(it.tags.get("time_grain") for it in result.items)

    # filter 候选只取前两个值,值原样透传。
    filter_query = next(q for q in captured if q.filters)
    assert filter_query.filters[0]["field"] == "loan.status"
    assert filter_query.filters[0]["op"] == "="
    assert filter_query.filters[0]["value"] in {"A", "B"}

    # top_n 候选:按 metric 降序 + limit 5。
    top_query = next(q for q in captured if q.order_by)
    assert top_query.order_by == [{"column": "loan_amount", "direction": "desc"}]
    assert top_query.limit == 5


def test_natural_name_fallbacks():
    from trove.services.evalset.templates import natural_name

    assert natural_name("地区", "x", ["y"], "district") == "地区"
    assert natural_name("", "地区代码", ["y"], "district") == "地区代码"
    assert natural_name("", "这是一段超过二十个字符的描述文本所以要跳过它", ["别名"], "d") == "别名"
    assert natural_name("", "", [], "loan_amount") == "loan amount"


def test_candidate_key_is_component_join():
    cand = Candidate(kind="metric_dim", metric="m", dimension="d")
    assert cand.key() == "metric_dim|m|d||||0"


async def test_rewrite_success_replaces_questions(monkeypatch):
    class GoodLLM:
        def __init__(self):
            self.messages = None

        async def chat(self, **kw):
            self.messages = kw["messages"]
            import json
            n = len(json.loads(kw["messages"][1]["content"].split("：\n", 1)[1]))
            return "```json\n" + json.dumps(
                [f"改写问句 {i}" for i in range(n)], ensure_ascii=False
            ) + "\n```"

    llm = GoodLLM()
    monkeypatch.setattr("trove.services.evalset.generate.build_and_compile",
                        lambda *a, **k: {"sql": "SELECT 1", "columns": [],
                                         "datasets": [], "version": 1})
    result = await generate_semantic(
        ctx(llm=llm, draft_model="mock/model"), limit=2, rewrite=True)
    assert [it.question for it in result.items] == ["改写问句 0", "改写问句 1"]
    assert not any(d["code"] == "rewrite_failed" for d in result.degraded)
    assert llm.messages[0]["role"] == "system"


# ── 追加:production / synthetic 采集(Task 6)─────────────────────

import json

import pytest

from trove.services.evalset.sources import collect_production, generate_synthetic


class FakeAudit:
    def __init__(self, rows=None, refusals=None):
        self.rows, self.refusals = rows or [], refusals or {}

    async def aggregate_query_audit(self, *, datasource, since="", limit=200):
        return self.rows

    async def aggregate_refusal_audit(self, *, since="", scan_limit=2000):
        return self.refusals


@pytest.fixture
def episode_db(tmp_path):
    path = tmp_path / "memory" / "episodes.sqlite"
    path.parent.mkdir(parents=True)
    return path


async def test_production_classification(episode_db, tmp_path):
    from tests.services.evalset.fakes import seed_episodes  # helper added in Step 3
    await seed_episodes(episode_db, [
        {"question": "ok question", "sql": "SELECT 1", "verdict": "OK"},
        {"question": "empty question", "sql": "SELECT 2", "verdict": "EMPTY"},
        {"question": "retry question", "sql": "SELECT 3", "verdict": "RETRY: x"},
        {"question": "corrected question", "sql": "SELECT 4", "verdict": "OK",
         "corrections": [{"kind": "value"}]},
    ])
    audit = FakeAudit(rows=[
        {"question": "audit ok", "sql": "SELECT 5", "verdict": "OK",
         "error": "", "last_seen": "2026-10-19", "seen": 2},
        {"question": "audit empty", "sql": "SELECT 6", "verdict": "EMPTY",
         "error": "", "last_seen": "2026-10-19", "seen": 1},
        {"question": "audit error", "sql": "", "verdict": "",
         "error": "boom", "last_seen": "2026-10-19", "seen": 1},
    ], refusals={"total": 1, "capped": False, "entries": [
        {"ts": "x", "datasource": "demo", "question": "refused question",
         "reason": "no_model"},
        {"ts": "x", "datasource": "other", "question": "other ds",
         "reason": "no_model"},
    ]})
    result = await collect_production(
        ctx(home_dir=tmp_path, auth_store=audit), limit=50)
    by_q = {it.question: it for it in result.items}
    assert by_q["ok question"].needs_reference is False
    assert by_q["empty question"].needs_reference is True
    assert by_q["retry question"].needs_reference is True
    assert by_q["corrected question"].needs_reference is True
    assert by_q["audit ok"].needs_reference is False
    assert by_q["audit empty"].needs_reference is True
    assert by_q["audit error"].needs_reference is True
    assert by_q["refused question"].needs_reference is True
    assert "other ds" not in by_q
    assert all(it.source == "production" for it in result.items)
    assert all(it.created_by == "system" for it in result.items)
    # 隐私:产物不含任何用户字段
    blob = json.dumps([it.__dict__ for it in result.items], default=str)
    for field in ("user_id", "username", "principal"):
        assert field not in blob


async def test_answerable_version_beats_refusal(tmp_path):
    audit = FakeAudit(rows=[{"question": "same question", "sql": "SELECT 1",
                             "verdict": "OK", "error": "",
                             "last_seen": "x", "seen": 1}],
                      refusals={"total": 1, "capped": False, "entries": [
                          {"ts": "x", "datasource": "demo",
                           "question": "same question", "reason": "no_model"}]})
    result = await collect_production(ctx(home_dir=tmp_path, auth_store=audit), limit=10)
    assert [it.question for it in result.items] == ["same question"]
    assert result.items[0].needs_reference is False


async def test_capped_refusals_are_visible(tmp_path):
    audit = FakeAudit(refusals={"total": 5000, "capped": True, "entries": [
        {"ts": "x", "datasource": "demo", "question": "q", "reason": "r"}]})
    result = await collect_production(ctx(home_dir=tmp_path, auth_store=audit), limit=10)
    assert any(d["code"] == "refusal_scan_capped" for d in result.degraded)


async def test_synthetic_without_llm_is_not_success(tmp_path):
    result = await generate_synthetic(ctx(home_dir=tmp_path, llm=None), limit=10)
    assert result.status == "no_llm" and result.items == []


async def test_synthetic_uses_guard(monkeypatch):
    from trove.services.evalset import sources as s

    async def fake_gen(llm, model, tables, *, samples=None, stats=None, lang="en"):
        return [{"question": "q1", "sql": "SELECT 1", "tags": ["t"]},
                {"question": "q2", "sql": "BAD", "tags": []}]

    async def fake_validate(examples, registry, ds):
        return [e for e in examples if e["sql"] != "BAD"]

    monkeypatch.setattr(s, "generate_synthetic_examples", fake_gen)
    monkeypatch.setattr(s, "validate_examples", fake_validate)
    result = await generate_synthetic(
        ctx(llm=object(), tables=[{"name": "t"}]), limit=10)
    assert [it.question for it in result.items] == ["q1"]
    assert result.items[0].source == "synthetic"
    assert result.items[0].reference_origin == "llm"


def test_verdict_head_matches_distill():
    """本地副本必须与 history_distill 的口径逐例相等(跨层不 import 的纪律)。"""
    from trove.services.evalset.sources import _verdict_head
    from trove.services.kb.history_distill import _verdict_head as distill_head

    for verdict in ("", "OK", "ok", "EMPTY", "RETRY: x", "NO_SQL", "fail",
                    " OK trailing", "EMPTY\n", "RETRY:x", "   ", "123"):
        assert _verdict_head(verdict) == distill_head(verdict), verdict


async def test_episode_wins_over_audit_for_same_question(episode_db, tmp_path):
    """精确 (question.lower, sql) 相同 → 只留 episode 版本(不重复收录)。"""
    from tests.services.evalset.fakes import seed_episodes
    await seed_episodes(episode_db, [
        {"question": "Same question", "sql": "SELECT 1", "verdict": "OK"},
    ])
    audit = FakeAudit(rows=[{"question": "same question", "sql": "SELECT 1",
                             "verdict": "RETRY: x", "error": "",
                             "last_seen": "x", "seen": 1}])
    result = await collect_production(ctx(home_dir=tmp_path, auth_store=audit), limit=10)
    assert [(it.question, it.reference_sql) for it in result.items] == \
        [("Same question", "SELECT 1")]


async def test_audit_different_sql_survives_episode_failure(episode_db, tmp_path):
    """episode 失败 + 同题不同 SQL 的审计 OK → 可答版本必须保留(R10)。"""
    from tests.services.evalset.fakes import seed_episodes
    await seed_episodes(episode_db, [
        {"question": "Q", "sql": "SELECT 1", "verdict": "RETRY: x"},
    ])
    audit = FakeAudit(rows=[{"question": "q", "sql": "SELECT 2",
                             "verdict": "OK", "error": "",
                             "last_seen": "x", "seen": 1}])
    result = await collect_production(ctx(home_dir=tmp_path, auth_store=audit), limit=10)
    assert [(it.question, it.reference_sql, it.needs_reference)
            for it in result.items] == [
        ("Q", "", True),            # episode 失败版(保留 verdict 注记)
        ("q", "SELECT 2", False),   # 后来答上的审计版本
    ]


async def test_episode_dedupe_prefers_reference_sql(episode_db, tmp_path):
    """同一问题的 episode:近期的 RETRY 不能顶掉更早带参考 SQL 的成功版(R11)。"""
    from tests.services.evalset.fakes import seed_episodes
    await seed_episodes(episode_db, [
        {"question": "Q", "sql": "SELECT 1", "verdict": "OK"},
        {"question": "Q", "sql": "", "verdict": "RETRY: x"},
    ])
    result = await collect_production(ctx(home_dir=tmp_path), limit=10)
    assert [(it.question, it.reference_sql, it.needs_reference)
            for it in result.items] == [("Q", "SELECT 1", False)]


async def test_production_respects_limit(episode_db, tmp_path):
    from tests.services.evalset.fakes import seed_episodes
    await seed_episodes(episode_db, [
        {"question": f"q{i}", "sql": f"SELECT {i}", "verdict": "OK"}
        for i in range(3)
    ])
    result = await collect_production(ctx(home_dir=tmp_path), limit=2)
    assert len(result.items) == 2


async def test_production_empty_history_is_empty_ok(tmp_path):
    result = await collect_production(ctx(home_dir=tmp_path), limit=10)
    assert result.items == [] and result.status == "ok"


async def test_collect_history_output_unchanged_by_evalset(episode_db, tmp_path):
    from trove.services.kb.history_distill import collect_history
    from tests.services.evalset.fakes import seed_episodes
    await seed_episodes(episode_db, [
        {"question": "q", "sql": "SELECT 1", "verdict": "OK"},
    ])
    rows = await collect_history("demo", home_dir=tmp_path, project_root=tmp_path)
    assert [(r.question, r.sql, r.verdict, r.source) for r in rows] == \
        [("q", "SELECT 1", "OK", "episode")]
