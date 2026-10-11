"""E4 · 自然键接地断言在执行面上的落点(本体形态批次,设计 §5.2)。

单元面的判定在 ``tests/services/semantic_layer/test_natural_key_grounding.py``;
这里钉的是**节点接线**:断言接在既有编译保真纪律之后、执行之前,失败走既有的
``_compile_drift_failure`` 打回通道,未声明时反馈文本逐字节不变。

承重场景是 **partial 骨架**(软 MISS):``skeleton_preserved`` 的条件检查是
"骨架 ⊆ 生成"(生成侧**加**一条过滤是合法补缺),于是"加一条编出来的自然键
过滤"恰好从这条缝里穿过去 —— 全量编译路径上签名会同时比对过滤值,本断言
是纵深防御,不是唯一防线。
"""
from __future__ import annotations

from trove.services.semantic_layer.contract import (
    PlanContract,
    contract_to_wire,
)
from trove.workflow.nodes.execute_sql import make_execute_sql
from trove.workflow.state import WorkflowState


def make_state(**kwargs) -> WorkflowState:
    defaults = {"session_id": "s1", "question": "Alice 的成绩是多少"}
    defaults.update(kwargs)
    return WorkflowState(**defaults)


def _partial_contract(*, literal: str, natural_keys=()):
    """partial 契约:骨架 WHERE 就是这条等值条件(生成侧照留即可过结构门)。"""
    where = ((("students", "name"),), "eq", (literal,))
    return contract_to_wire(PlanContract(
        skeleton_sql=(
            "SELECT COUNT(name)\n"
            f"FROM students\nWHERE students.name = '{literal}'"
        ),
        partial=True,
        where=(where,),
        natural_keys=tuple(natural_keys),
    ))


class TestNaturalKeyGroundingAtExecution:
    """执行前打回:键值编造 → COMPILE_DRIFT;接地 → 照常执行。"""

    async def test_invented_literal_is_blocked_before_execution(self, sqlite_registry):
        """问 Alice、骨架里写着 name='李四' → 执行前打回,不落库。"""
        node = make_execute_sql(sqlite_registry)
        state = make_state(
            sql="SELECT COUNT(name) FROM students WHERE students.name = '李四'",
            compiled=True,
            compile_partial=True,
            compiled_sql="SELECT COUNT(name) FROM students WHERE students.name = '李四'",
            contract=_partial_contract(
                literal="李四", natural_keys=[("students", "name")]),
        )
        update = await node(state)
        feedback = update["error_feedback"]
        assert "COMPILE_DRIFT" in feedback
        assert "Reason:" in feedback
        assert "'李四'" in feedback
        assert update["retry_count"] == 1
        assert update["row_count"] == -1  # 本轮未执行
        assert update["columns"] == []  # 无执行产物

    async def test_grounded_literal_executes(self, sqlite_registry):
        """正例(施工单):提问含 Alice、条件 name='Alice' → 放行执行。"""
        node = make_execute_sql(sqlite_registry)
        state = make_state(
            sql="SELECT COUNT(name) FROM students WHERE students.name = 'Alice'",
            compiled=True,
            compile_partial=True,
            compiled_sql="SELECT COUNT(name) FROM students WHERE students.name = 'Alice'",
            contract=_partial_contract(
                literal="Alice", natural_keys=[("students", "name")]),
        )
        update = await node(state)
        assert "COMPILE_DRIFT" not in update
        assert update["row_count"] == 1
        assert update["error_feedback"] == ""

    async def test_contract_without_natural_keys_keeps_status_quo(self, sqlite_registry):
        """未声明实体 → 契约无自然键 → 同一编造字面量照旧执行(兼容安全带)。"""
        node = make_execute_sql(sqlite_registry)
        state = make_state(
            sql="SELECT COUNT(name) FROM students WHERE students.name = '李四'",
            compiled=True,
            compile_partial=True,
            compiled_sql="SELECT COUNT(name) FROM students WHERE students.name = '李四'",
            contract=_partial_contract(literal="李四"),  # natural_keys 为空
        )
        update = await node(state)
        assert "COMPILE_DRIFT" not in update
        assert update["row_count"] == 1  # COUNT 的 0 行仍是 1 条结果

    async def test_structural_drift_feedback_has_no_reason_line(self, sqlite_registry):
        """结构失败(丢 join)的反馈文本与改造前逐字节一致 —— 不带 Reason 行。

        Reason 行只随自然键断言的失败出现(它是这条断言的判词);结构保真
        的失败消息不因本批次而变。
        """
        node = make_execute_sql(sqlite_registry)
        skeleton = (
            "SELECT students.grade, COUNT(name)\n"
            "FROM students\n"
            "JOIN classes ON students.class_id = classes.id\n"
            "GROUP BY students.grade"
        )
        state = make_state(
            sql="SELECT students.grade, COUNT(name)\nFROM students\n"
                "GROUP BY students.grade",  # 删了 join
            compiled=True,
            compile_partial=True,
            compiled_sql=skeleton,
            contract=contract_to_wire(PlanContract(
                skeleton_sql=skeleton,
                partial=True,
                join_edges=((("classes", "id"), ("students", "class_id")),),
                natural_keys=(("students", "name"),),
            )),
        )
        update = await node(state)
        assert "COMPILE_DRIFT" in update["error_feedback"]
        assert "Reason:" not in update["error_feedback"]
        assert update["retry_count"] == 1
        assert update["row_count"] == -1

    async def test_budget_exhausted_degrades_to_error(self, sqlite_registry):
        """预算耗尽仍编造键值 → 优雅降级为 error(与既有 drift 降级同路)。"""
        node = make_execute_sql(sqlite_registry)
        state = make_state(
            sql="SELECT COUNT(name) FROM students WHERE students.name = '李四'",
            compiled=True,
            compile_partial=True,
            compiled_sql="SELECT COUNT(name) FROM students WHERE students.name = '李四'",
            contract=_partial_contract(
                literal="李四", natural_keys=[("students", "name")]),
            retry_count=10,
        )
        update = await node(state)
        assert "COMPILE_DRIFT" in update["error"]
        assert "Reason:" in update["error"]
        assert "error_feedback" not in update

    async def test_undeclared_full_compile_path_is_byte_identical(self, sqlite_registry):
        """全量编译 + 契约无自然键 → 与现状同一判定(签名门独立工作)。

        生成 SQL 与编译 SQL 一致 → 照抄门放行,自然键检查空转。
        """
        node = make_execute_sql(sqlite_registry)
        sql = "SELECT COUNT(name) FROM students WHERE students.name = 'Alice'"
        state = make_state(
            sql=sql,
            compiled=True,
            compiled_sql=sql,
            contract=contract_to_wire(PlanContract(skeleton_sql=sql)),
        )
        update = await node(state)
        assert "COMPILE_DRIFT" not in update
        assert update["row_count"] == 1
