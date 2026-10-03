"""输出节点 × 分析柱:驱动器树 markdown 表 + analysis 缺省回落。

``test_attribution.py`` 钉老路径(attribution-only 状态)不变;这里钉
**新增**部分:state.analysis 有树 → 渲染缩进分解表;没有 → 一个字节
都不多加(双写期的兼容面)。
"""

from trove.workflow.nodes.output import _build_attribution_section, output
from trove.workflow.state import WorkflowState

_ATTR = {
    "narrative": "主要是华东贡献",
    "baseline": "prev_period",
    "total_delta": -10.0,
    "table": [
        {"dim": "华东", "base": 100, "current": 80, "delta": -20, "contribution": -0.6},
    ],
}

_TREE = {
    "name": "profit", "kind": "derived", "op": "-", "decomposable": True,
    "current": 80.0, "base": 100.0, "delta": -20.0, "executed": True,
    "value_source": "hop0",
    "residual": {"value": 0.0, "exact": True, "reason": "identity"},
    "children": [
        {"name": "revenue", "kind": "leaf", "current": 90.0, "base": 130.0,
         "delta": -40.0, "executed": True},
        {"name": "cost", "kind": "leaf", "current": 10.0, "base": 30.0,
         "delta": -20.0, "executed": True},
    ],
}


def _state(**kw) -> WorkflowState:
    base = {"session_id": "s1", "question": "为什么利润下降"}
    base.update(kw)
    return WorkflowState(**base)


class TestDriverTreeSection:
    def test_tree_rendered_indented_with_identity_note(self):
        state = _state(attribution=_ATTR, analysis={"tree": _TREE})
        section = _build_attribution_section(state)
        assert "驱动因素分解" in section
        # 缩进:根无前缀,子节点带全角空格
        assert "| profit |" in section
        assert "| 　revenue |" in section and "| 　cost |" in section
        # 根行:恒等式成立(残差精确)
        assert "恒等式成立" in section
        # 值:当前/基期/Δ 三列都对
        assert "| 　revenue | 130 | 90 | -40 |" in section

    def test_gap_and_unavailable_are_honest(self):
        tree = dict(_TREE)
        tree["residual"] = {"value": 3.0, "exact": False, "reason": "gap"}
        tree["children"] = [
            {"name": "revenue", "kind": "leaf", "current": 90.0, "base": 130.0,
             "delta": -40.0, "executed": True},
            {"name": "cost", "kind": "leaf", "executed": False, "note": "cycle"},
        ]
        tree["residual"] = {"value": None, "exact": False, "reason": "component_unavailable"}
        state = _state(attribution=_ATTR, analysis={"tree": tree})
        section = _build_attribution_section(state)
        assert "组件未取到、不声称分解" in section
        assert "cycle" in section  # 未执行节点带自己的 note
        assert "—" in section      # 无值列不编数

    def test_leaf_tree_not_rendered(self):
        leaf = {"name": "revenue", "kind": "leaf", "current": 1.0, "base": 2.0,
                "delta": -1.0, "executed": True, "children": []}
        state = _state(attribution=_ATTR, analysis={"tree": leaf})
        section = _build_attribution_section(state)
        assert "驱动因素分解" not in section  # 单叶无可拆 → 不添噪声

    def test_no_analysis_is_byte_identical_to_legacy(self):
        legacy = _state(attribution=_ATTR)
        with_analysis_empty = _state(attribution=_ATTR, analysis=None)
        assert (_build_attribution_section(legacy)
                == _build_attribution_section(with_analysis_empty))

    async def test_output_node_renders_tree(self):
        state = _state(attribution=_ATTR, analysis={"tree": _TREE})
        out = await output(state)
        assert "驱动因素分解" in out["final_response"]
        assert "归因分析" in out["final_response"]
