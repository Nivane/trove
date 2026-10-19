"""逐题回放:零执行、零额外 LLM、重规划上限、状态合并纪律。

引擎不回放 gen_sql/执行面 —— 这些测试用一个 ``execute`` 必抛的适配器
证明它一次都没被调用;节点用 stub 钉住「引擎只信节点产出的状态」。
"""

from __future__ import annotations

import pytest

from trove.core.config import AgentConfig
from trove.core.types import ColumnInfo, SchemaInfo, TableInfo
from trove.services.question_coverage import engine as engine_mod
from trove.services.question_coverage.engine import (
    CoverageBudgetExceeded,
    CoverageEngine,
    CountingLLM,
    Prepared,
    merge_update,
)
from trove.workflow.state import WorkflowState


class _Adapter:
    def __init__(self) -> None:
        self.execute_calls = 0

    def dialect(self) -> str:
        return "mysql"

    async def get_schema(self) -> SchemaInfo:
        return SchemaInfo(tables=[TableInfo(name="loan", columns=[
            ColumnInfo(name="amount", type="decimal")])])

    async def execute(self, *args, **kwargs):
        self.execute_calls += 1
        raise AssertionError("coverage must never execute SQL")


class _Provider:
    enabled = True

    def model(self):
        return None


class _Registry:
    default_name = "demo"

    def __init__(self, adapter: _Adapter) -> None:
        self.adapter = adapter

    async def get(self, datasource=None):
        return self.adapter

    def allowed_tables(self, datasource=None):
        return None


class _KB:
    def semantics_path(self, datasource: str):
        return None


class _LLM:
    def __init__(self, responses=None) -> None:
        self.responses = list(responses or ["plan"])
        self.calls = 0
        self.models: list[str] = []

    async def chat(self, *args, **kwargs):
        self.calls += 1
        self.models.append(str(kwargs.get("model", args[0] if args else "")))
        return self.responses.pop(0) if self.responses else "plan"


class _FailingLLM:
    calls = 0

    async def chat(self, *args, **kwargs):
        type(self).calls += 1
        raise RuntimeError("provider down")


async def _noop(state):
    return {}


def _compiled(**extra):
    async def node(state):
        return {
            "compile_meta": {"outcome": "compiled", "plan_typed": True},
            "plan": "plan text",
            "compiled_sql": "SELECT 1",
            **extra,
        }
    return node


class _Harness:
    def __init__(
        self,
        monkeypatch,
        *,
        parse=_noop,
        link=_noop,
        fast=None,
        sketch_factory=None,
        llm=None,
        max_calls=6,
    ) -> None:
        self.adapter = _Adapter()
        self.llm = llm if llm is not None else _LLM(["plan"])
        self.calls = {"parse": 0, "link": 0, "fast": 0}
        self.sketch_kwargs: list[dict] = []

        async def _fast(state):
            self.calls["fast"] += 1
            return {"fast_path": False}

        fast_node = fast
        if fast_node is None:
            fast_node = _fast
        elif not isinstance(fast_node, type(_noop)):
            fast_update = fast
            async def fast_node(state):  # noqa: F811
                self.calls["fast"] += 1
                return fast_update

        async def _link(state):
            self.calls["link"] += 1
            return await link(state) if link is not _noop else {}
        async def _parse(state):
            self.calls["parse"] += 1
            return await parse(state) if parse is not _noop else {}

        self._sketch_factory = sketch_factory

        def make_sketch(llm, config, **kwargs):
            self.sketch_kwargs.append(kwargs)
            if self._sketch_factory is not None:
                return self._sketch_factory(llm, config, **kwargs)
            return _compiled()

        monkeypatch.setattr(engine_mod, "make_parse_date", lambda *a, **k: _parse)
        monkeypatch.setattr(engine_mod, "make_schema_linking", lambda *a, **k: _link)
        monkeypatch.setattr(engine_mod, "make_fast_match", lambda *a, **k: fast_node)
        monkeypatch.setattr(engine_mod, "make_query_sketch", make_sketch)

        self.engine = CoverageEngine(
            kb=_KB(), connectors=_Registry(self.adapter), llm=self.llm,
            config=AgentConfig(target="target/m", model_fast="fast/m"),
            datasource="demo", run_id="r1", max_llm_calls=max_calls,
        )
        self.engine.configure(Prepared(
            adapter=self.adapter, schema=None, provider=_Provider(),
            schema_map={"loan": {"amount"}},
        ))

    async def check(self, question="how many loans?", seq=0):
        return await self.engine.check(question, seq=seq)


# ── 短路径:规则零 LLM ──────────────────────────────────────────────────


async def test_not_query_never_touches_nodes_or_llm(monkeypatch):
    h = _Harness(monkeypatch)
    result = await h.check("delete from loan")
    assert result["outcome"] == "not_query"
    assert result["outcome_reason"] == "write"
    assert result["llm_calls"] == 0
    assert h.calls == {"parse": 0, "link": 0, "fast": 0}
    assert h.llm.calls == 0
    assert h.adapter.execute_calls == 0


async def test_certified_fast_path_is_zero_llm(monkeypatch):
    h = _Harness(monkeypatch, fast={
        "fast_path": True, "sql": "SELECT COUNT(*) FROM loan",
        "kb_hits": [{"question": "how many loans"}],
    })
    result = await h.check()
    assert result["outcome"] == "certified"
    assert result["template_name"] == "how many loans"
    assert result["detail"]["sql_preview"].startswith("SELECT COUNT")
    assert result["llm_calls"] == 0
    assert h.llm.calls == 0
    assert h.adapter.execute_calls == 0


async def test_no_model_is_unmatched_with_a_dataset_gap(monkeypatch):
    async def link(state):
        return {"no_model": True}

    h = _Harness(monkeypatch, link=link)
    result = await h.check()
    assert result["outcome"] == "unmatched"
    assert result["outcome_reason"] == "no_model"
    assert result["llm_calls"] == 0
    assert result["gaps"][0]["kind"] == "dataset"
    assert result["gaps"][0]["workbench"] == "/admin/kb?ds=demo"
    assert h.llm.calls == 0


async def test_no_semantic_match_is_unmatched_zero_llm(monkeypatch):
    async def link(state):
        return {"refusal": {"reason": "no_semantic_match"}}

    h = _Harness(monkeypatch, link=link)
    result = await h.check()
    assert result["outcome"] == "unmatched"
    assert result["outcome_reason"] == "no_semantic_match"
    assert h.llm.calls == 0


# ── 计划回放 ───────────────────────────────────────────────────────────


async def test_compiled_injects_schema_map_and_reports_gaps_free(monkeypatch):
    h = _Harness(monkeypatch)
    result = await h.check()
    assert result["outcome"] == "compiled"
    assert result["gaps"] == []
    assert result["miss_reason"] == ""
    assert h.sketch_kwargs[0]["schema_map"] == {"loan": {"amount"}}
    assert h.sketch_kwargs[0]["max_retries"] == 10
    assert h.adapter.execute_calls == 0


async def test_partial_keeps_three_key_misses_with_value(monkeypatch):
    async def node(state):
        return {
            "compile_meta": {
                "outcome": "partial", "plan_typed": True,
                "partial_reasons": ["enum_value_unresolved"],
            },
            "compile_misses": [{
                "reason": "enum_value_unresolved",
                "component": "loan.amount", "value": "A",
            }],
        }

    h = _Harness(monkeypatch, sketch_factory=lambda *a, **k: node)
    result = await h.check()
    assert result["outcome"] == "partial"
    assert result["outcome_reason"] == "enum_value_unresolved"
    assert result["gaps"][0]["reason"] == "enum_value_unresolved"
    assert result["gaps"][0]["value"] == "A"


async def test_soft_miss_has_no_value_gap(monkeypatch):
    async def node(state):
        return {"compile_meta": {
            "outcome": "miss", "miss_class": "soft",
            "miss_reason": "missing_filter_value",
            "miss_component": "loan.amount",
        }}

    h = _Harness(monkeypatch, sketch_factory=lambda *a, **k: node)
    result = await h.check()
    assert result["outcome"] == "soft_miss"
    assert result["gaps"][0]["value"] == ""
    assert result["gaps"][0]["draftable"] is False


async def test_refused_carries_the_relation_gap_and_deep_link(monkeypatch):
    miss = {"reason": "unknown_cardinality", "component": "loan, account"}

    async def node(state):
        return {
            "compile_meta": {
                "outcome": "miss", "miss_class": "hard",
                "miss_reason": "unknown_cardinality",
                "miss_component": "loan, account",
            },
            "refusal": {"reason": "uncovered", "compile_miss": dict(miss)},
        }

    h = _Harness(monkeypatch, sketch_factory=lambda *a, **k: node)
    result = await h.check()
    assert result["outcome"] == "refused"
    assert result["miss_class"] == "hard"
    gap = result["gaps"][0]
    assert gap["kind"] == "relation"
    assert gap["workbench"] == "/admin/semantic?ds=demo&tab=datasets&q=loan"
    assert gap["draftable"] is False


async def test_plan_validation_dropped_is_unplanned(monkeypatch):
    async def node(state):
        return {"plan_validation": {"status": "dropped", "errors": ["x"]},
                "plan_replan_pending": False}

    h = _Harness(monkeypatch, sketch_factory=lambda *a, **k: node)
    result = await h.check()
    assert result["outcome"] == "unplanned"
    assert result["outcome_reason"] == "plan_validation_dropped"


async def test_empty_plan_after_a_real_call_is_unplanned(monkeypatch):
    def factory(llm, *a, **k):
        async def node(state):
            await llm.chat(model="m", messages=[])
            return {"plan_replan_pending": False}
        return node

    h = _Harness(monkeypatch, llm=_LLM(["{}"]), sketch_factory=factory)
    result = await h.check()
    assert result["outcome"] == "unplanned"
    assert result["outcome_reason"] == "empty_plan"
    assert result["llm_calls"] == 1
    assert result["detail"]["llm_models"] == ["m"]


async def test_zero_call_without_meta_is_sketch_failed_before_llm(monkeypatch):
    async def node(state):
        return {"plan_replan_pending": False}

    h = _Harness(monkeypatch, sketch_factory=lambda *a, **k: node)
    result = await h.check()
    assert result["outcome"] == "error"
    assert result["outcome_reason"] == "sketch_failed_before_llm"


async def test_llm_failure_is_error_llm_error_not_a_miss(monkeypatch):
    def factory(llm, *a, **k):
        async def node(state):
            try:
                await llm.chat(model="m", messages=[])
            except Exception:
                pass  # 生产节点的外层 except 也这样吞掉
            return {"plan_replan_pending": False}
        return node

    h = _Harness(
        monkeypatch, llm=_FailingLLM(), sketch_factory=factory)
    result = await h.check()
    assert result["outcome"] == "error"
    assert result["outcome_reason"] == "llm_error"
    assert "provider down" in result["detail"]["last_call_error"]


# ── 重规划 ─────────────────────────────────────────────────────────────


async def test_replan_converges_after_one_more_invocation(monkeypatch):
    calls = {"n": 0}

    async def node(state):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"plan_replan_pending": True, "error_feedback": "fix it"}
        return {"compile_meta": {"outcome": "compiled", "plan_typed": True},
                "plan_replan_pending": False}

    h = _Harness(monkeypatch, sketch_factory=lambda *a, **k: node)
    result = await h.check()
    assert result["outcome"] == "compiled"
    assert calls["n"] == 2
    assert result["detail"]["invocations"] == 2


async def test_replan_exhaustion_is_a_loud_error(monkeypatch):
    async def node(state):
        return {"plan_replan_pending": True}

    h = _Harness(monkeypatch, sketch_factory=lambda *a, **k: node)
    result = await h.check()
    assert result["outcome"] == "error"
    assert result["outcome_reason"] == "replan_not_converged"
    assert result["detail"]["invocations"] == 3


# ── 状态合并纪律 ────────────────────────────────────────────────────────


def test_merge_update_drops_unknown_fields_and_reports_them():
    state = WorkflowState(session_id="s", question="q")
    merged, dropped = merge_update(state, {
        "plan": "p", "not_a_state_field": 1,
    })
    assert merged.plan == "p"
    assert dropped == ["not_a_state_field"]
    assert not hasattr(merged, "not_a_state_field")


async def test_unknown_update_keys_land_in_detail_dropped_fields(monkeypatch):
    h = _Harness(
        monkeypatch,
        sketch_factory=lambda *a, **k: _compiled(bogus_key=True))
    result = await h.check()
    assert result["outcome"] == "compiled"
    assert result["detail"]["dropped_fields"] == ["bogus_key"]


# ── 预算 ───────────────────────────────────────────────────────────────


async def test_budget_exhaustion_survives_the_nodes_swallow(monkeypatch):
    def factory(llm, *a, **k):
        async def node(state):
            try:
                await llm.chat(model="m", messages=[])
                await llm.chat(model="m", messages=[])
            except CoverageBudgetExceeded:
                pass  # 生产节点的 ``except Exception`` 会这样吞掉
            return {"plan_replan_pending": False}
        return node

    h = _Harness(
        monkeypatch, llm=_LLM(["a", "b"]), max_calls=1,
        sketch_factory=factory)
    with pytest.raises(CoverageBudgetExceeded):
        await h.check()


async def test_counting_llm_clears_the_error_on_a_successful_retry():
    llm = _LLM(["ok"])
    counter = CountingLLM(llm, cap=5)
    with pytest.raises(RuntimeError):
        await CountingLLM(_FailingLLM(), cap=5).chat(model="m", messages=[])
    assert counter.last_call_error == ""
    await counter.chat(model="m", messages=[])
    assert counter.calls == 1
    assert counter.models == ["m"]
    assert counter.last_call_error == ""


def test_engine_model_matches_the_node_expression(monkeypatch):
    h = _Harness(monkeypatch)
    assert h.engine.model == "fast/m"
    assert h.engine.model_source == "model_fast"
