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
