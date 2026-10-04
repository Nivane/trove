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


_SERIES = {
    "grain": "month", "mode": "trailing", "lookback": 12,
    "span": ["2024-01-01", "2024-12-31"],
    "labels": ["2024-01", "2024-02"],
    "values": [40.0, 42.0],
    "band": {"center": 41.0, "scale": 2.0, "lo": 34.0, "hi": 48.0,
             "n": 12, "method": "robust", "degraded": []},
    "current": 70.0, "z": 14.5, "outside": True, "low_n": False,
    "k": 3.5, "confidence": 1.0,
}


#: 一份**完整的 v1 payload**(B8 之前分析包产物的逐键形状,B1 起 series
#: 就是可选节,所以 v1 里可能有、也可能没有 —— 这里取没有的那支):
#: 老读端遇到的新键原样忽略、新读端遇到 v1 缺的键整段不渲染,
#: 缺席容忍就是兼容机制本身(无迁移)。
_V1_PAYLOAD = {
    "version": 1, "kind": "driver_tree", "metric": "profit",
    "metric_kind": "additive",
    "labels": {"question": "为什么利润下降", "baseline": "prev_period",
               "baseline_label": "上期", "primary_dimension": "",
               "dimensions": []},
    "total_delta": -20.0, "table": [], "effects": None, "drilldown": None,
    "tree": _TREE, "charts": [],
    "evidence": {"datasource": "demo", "queries": [], "truncated": False,
                 "degraded": []},
    "partial": False,
}


class TestNoiseBandSection:
    """噪声带段(B8):三态如实 / 位置分数带非概率限定语 / 缺席不动老路径。

    v1 payload fixture(无 series 键)必须与老路径逐字节一致 —— 这就是
    版本兼容的机制本身(缺席容忍,不是迁移)。渲染判据是**键在不在**,
    不是版本号:v1 也可能带 series(B1 起可选节),照样渲染。
    """

    def test_v1_payload_fixture_renders_unchanged(self):
        """完整 v1 payload fixture(有 tree、无 series)= 现状输出,逐字节。"""
        legacy = _state(attribution=_ATTR, analysis={"tree": _TREE})
        v1 = _state(attribution=_ATTR, analysis=dict(_V1_PAYLOAD))
        section = _build_attribution_section(v1)
        assert section == _build_attribution_section(legacy)
        assert "噪声带" not in section

    def test_v1_payload_with_series_still_renders_band(self):
        """判据是键在不在:老版本号带 series 的 payload 也照常渲染。"""
        v1 = dict(_V1_PAYLOAD, series=dict(_SERIES))
        section = _build_attribution_section(
            _state(attribution=_ATTR, analysis=v1))
        assert "**噪声带**" in section and "超出噪声带" in section

    def test_outside_band_full_line(self):
        state = _state(attribution=_ATTR, analysis={"series": dict(_SERIES)})
        section = _build_attribution_section(state)
        assert "**噪声带**" in section
        assert "近 12 个月块" in section
        assert "中位数 41" in section and "带 [34, 48]" in section
        assert "本期值 70" in section
        assert "稳健 z=14.50" in section
        assert "位置分数 1.00（非概率）" in section   # 限定语不可省
        assert "超出噪声带" in section

    def test_inside_band(self):
        s = dict(_SERIES, current=45.0, z=2.0, outside=False, confidence=0.0)
        section = _build_attribution_section(
            _state(attribution=_ATTR, analysis={"series": s}))
        assert "落在噪声带内" in section

    def test_unusable_band_says_why_and_undecidable(self):
        s = dict(_SERIES, band={
            "center": None, "scale": None, "lo": None, "hi": None,
            "n": 3, "method": "robust", "degraded": ["insufficient_n"]},
            outside=None, low_n=True, z=None, confidence=None)
        section = _build_attribution_section(
            _state(attribution=_ATTR, analysis={"series": s}))
        assert "噪声带不可用（样本不足）" in section   # 原因译成人话,不出原始键
        assert "→ 判不了" in section
        assert "insufficient_n" not in section

    def test_missing_current_no_verdict_clause(self):
        s = dict(_SERIES, current=None, z=None, outside=None, confidence=None)
        section = _build_attribution_section(
            _state(attribution=_ATTR, analysis={"series": s}))
        assert "本期值缺失" in section
        assert "→" not in section.split("**噪声带**")[1].split("\n")[0]

    def test_low_n_note(self):
        s = dict(_SERIES, low_n=True)
        section = _build_attribution_section(
            _state(attribution=_ATTR, analysis={"series": s}))
        assert "样本不足，带估计仅供参考" in section

    def test_en_rendering(self):
        state = _state(attribution=_ATTR, analysis={"series": dict(_SERIES)},
                       lang="en")
        section = _build_attribution_section(state)
        assert "**Noise band**" in section
        assert "over the last 12 month-blocks" in section
        assert "position score 1.00 (not a probability)" in section
        assert "outside the noise band" in section

    def test_band_outside_missing_key_is_undecidable_not_crash(self):
        """坏数据不炸:outside 是字符串等非布尔 → 判不了(诚实),不是抛错。"""
        s = dict(_SERIES, outside="yes")
        section = _build_attribution_section(
            _state(attribution=_ATTR, analysis={"series": s}))
        assert "→ 判不了" in section
