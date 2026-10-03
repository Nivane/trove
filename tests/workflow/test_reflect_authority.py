"""reflect 的权威编译产物豁免(P3.5 机制 ①;0492 死因)。

判据(``authoritative_compiled_sql``,与 validate 的改写型规则豁免同一份)
成立时,**复杂度档比较被旁路**:编译产物 + 规则全过 = 语义层权威答案,
不该再交给 LLM 判官做语义再裁决。0492 实测:与 gold 逐字节相等的编译
结果,仅因 ``complexity=complex`` 超过 ``reflect_skip=standard`` 档被
判官(拿不准就 RETRY)打回,回滚后反而变错。

其余六项条件(rules_passed / 无 error_feedback / 无弱信号 / row_count>0 /
投影宽度自洽 / skip != off)逐字不动 —— ``off`` 仍尊重(运维显式要求全判),
判据任何一项读不到 → False → 判官照常跑(失效方向安全)。

权威 state 的拼法沿用 ``test_nodes.TestValidateAuthoritativeExemption``:
真实编译器(``_compile_semantic``)产出的 SQL + 契约 wire,不是手搓。
"""

from __future__ import annotations

from typing import Any

from trove.core.config import AgentConfig
from trove.services.semantic_layer.contract import contract_to_wire
from trove.workflow.nodes.authority import authoritative_compiled_sql
from trove.workflow.nodes.reflect import make_reflect
from trove.workflow.state import WorkflowState

SKIP_REASON = "deterministic rules passed; reflect skipped"


class RecordingLLM:
    """判官打桩:记录每次 chat 调用;返回脚本化裁决。

    **不用抛异常的打桩** —— reflect 把 LLM 异常吞成 ``{"verdict": "OK"}``
    (库内既有行为),那样"判官没跑"与"判官跑了但炸了"无法区分。
    调用清单是唯一可靠证据。
    """

    def __init__(self, response: str = "NO_SQL: definitional question") -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def chat(self, model, messages, **kwargs):
        self.calls.append({"model": model, "messages": messages})
        return self.response


def make_state(**kwargs) -> WorkflowState:
    defaults = {"session_id": "s1", "question": "how many loans are there"}
    defaults.update(kwargs)
    return WorkflowState(**defaults)


def _authoritative_state(**kw) -> WorkflowState:
    """真实编译器产出 (SQL, contract wire) 拼 reflect 的 state。

    默认形状即 0492 的致病组合:``complexity="complex"`` + 规则全过 +
    单行结果 + 投影宽度自洽 + 无 error_feedback —— 除复杂度档外全部满足。
    """
    from trove.services.semantic_layer.models import (
        SemanticDataset,
        SemanticField,
        SemanticMetric,
        SemanticModel,
    )
    from trove.workflow.nodes.query_sketch import _compile_semantic

    f = lambda name: SemanticField(name=name, expression=name)  # noqa: E731
    model = SemanticModel(
        name="fin",
        datasets=[SemanticDataset(name="loan", primary_key=["loan_id"], fields=[
            f("loan_id"), f("amount")])],
        metrics=[SemanticMetric("number of loan records", "COUNT(loan.loan_id)",
                                datasets=["loan"])],
    )

    class FakeProvider:
        enabled = True

        def model(self):
            return model

    plan = {"tables": ["loan"], "aggregation": "count(loan.loan_id)",
            "answer_columns": ["count(loan.loan_id)"], "conditions": []}
    compiled, miss = _compile_semantic(plan, ["loan"], FakeProvider(), "sqlite")
    assert miss is None and compiled is not None
    fields = {
        "question": "how many loans are there",
        "sql": compiled.sql,
        "columns": ["count(loan.loan_id)"],   # 1 列 == SQL 投影宽度
        "rows": [["1919"]],
        "row_count": 1,
        "compiled": True,
        "compiled_sql": compiled.sql,
        "compile_meta": {"outcome": "compiled"},
        "contract": contract_to_wire(compiled.contract),
        "complexity": "complex",
        "rules_passed": True,
    }
    fields.update(kw)  # 覆盖任一项以构造"判据缺失/不成立"的变体
    return make_state(**fields)


class TestAuthoritativePredicate:
    """迁移后的判据本体(``trove/workflow/nodes/authority.py``)直接单测。"""

    def test_full_compile_reproducing_contract_is_authoritative(self):
        ok, why = authoritative_compiled_sql(_authoritative_state())
        assert ok is True
        assert why == ""

    def test_partial_compile_is_not_authoritative(self):
        ok, why = authoritative_compiled_sql(_authoritative_state(compile_partial=True))
        assert ok is False
        assert why == "not a full compile"

    def test_missing_contract_is_not_authoritative(self):
        ok, why = authoritative_compiled_sql(_authoritative_state(contract=None))
        assert ok is False
        assert why == "no contract"

    def test_sql_not_reproducing_contract_is_not_authoritative(self):
        ok, _ = authoritative_compiled_sql(
            _authoritative_state(sql="SELECT name FROM students"))
        assert ok is False

    def test_non_compiled_outcome_is_not_authoritative(self):
        ok, why = authoritative_compiled_sql(
            _authoritative_state(compile_meta={"outcome": "partial"}))
        assert ok is False
        assert why == "outcome=partial"


class TestReflectAuthoritativeSkip:
    """0492 形状:复杂档 + 权威编译产物 → 跳过判官(零 LLM 调用)。"""

    async def test_0492_complex_authoritative_skips_judge(self):
        """complex > standard 档,但权威编译产物旁路档位比较 → 跳过。"""
        llm = RecordingLLM()
        node = make_reflect(llm, AgentConfig(target="mock/model", reflect_skip="standard"))
        update = await node(_authoritative_state())
        assert update["verdict"] == "OK"
        assert update["reason"] == SKIP_REASON
        assert llm.calls == []  # 判官从未被咨询

    async def test_partial_compile_keeps_judge(self):
        """骨架编译(compile_partial)不是权威照抄对象 → 档位比较照常,判官跑。"""
        llm = RecordingLLM()
        node = make_reflect(llm, AgentConfig(target="mock/model", reflect_skip="standard"))
        update = await node(_authoritative_state(compile_partial=True))
        assert len(llm.calls) == 1
        assert update["verdict"] == "NO_SQL"  # 裁决来自法官,不是跳过门
        assert update.get("reason") != SKIP_REASON

    async def test_rules_not_passed_keeps_judge(self):
        """规则没过 → 豁免不适用(权威判据只是复杂度档的旁路,不替代规则门)。"""
        llm = RecordingLLM()
        node = make_reflect(llm, AgentConfig(target="mock/model", reflect_skip="standard"))
        update = await node(_authoritative_state(rules_passed=False))
        assert len(llm.calls) == 1
        assert update["verdict"] == "NO_SQL"
        assert update.get("reason") != SKIP_REASON

    async def test_reflect_skip_off_keeps_judge(self):
        """off 是运维显式要求全判 → 权威产物也不跳。"""
        llm = RecordingLLM()
        node = make_reflect(llm, AgentConfig(target="mock/model", reflect_skip="off"))
        update = await node(_authoritative_state())
        assert len(llm.calls) == 1
        assert update["verdict"] == "NO_SQL"
        assert update.get("reason") != SKIP_REASON

    async def test_missing_contract_keeps_judge(self):
        """契约读不到 → 判据 False → 判官照常跑(失效方向安全)。"""
        llm = RecordingLLM()
        node = make_reflect(llm, AgentConfig(target="mock/model", reflect_skip="standard"))
        update = await node(_authoritative_state(contract=None))
        assert len(llm.calls) == 1
        assert update["verdict"] == "NO_SQL"
        assert update.get("reason") != SKIP_REASON
