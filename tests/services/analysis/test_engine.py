"""分析引擎编排单测:记录型 fake runner(零 I/O、零 LLM、零网络)。

断言四件事:
  - 跳序与 SQL 形状(hop0 → 维度探测 → hop1 → 驱动器树),维度预选重排;
  - 驱动器树值落位 + 诚实残差(加性恒等式精确;减法按算子带符号);
  - 降级记账(不可解析 / 字段遮蔽 / 预算截断)进 degraded,partial 如实;
  - ``analysis_payload`` 形状(kind / evidence / partial)。

runner 按 SQL 形状回罐头数字(自洽:区域行之和 == 总量),周期靠当前
窗口起始字面量 ``>= '2024-02-01'`` 区分(单日窗口,基期 = 2024-01-31;
半开区间下基期 SQL 含 ``< '2024-02-01'``,所以必须认 ``>=`` 一侧)。
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from trove.services.analysis.engine import (
    AnalysisEngine,
    AnalysisLimits,
    AnalysisOutcome,
    AnalysisRequest,
    _record_rows,
    analysis_payload,
)
from trove.services.semantic_layer.models import (
    SemanticDataset,
    SemanticField,
    SemanticMetric,
    SemanticModel,
)

# ── 罐头数字(自洽:总量 == 区域之和;树子项之和 == 父项)────────
# net = revenue − expense(加性恒等式);profit = revenue − cost 里
# "cost" 与声明字段同名 → 预检应剔除(字段遮蔽)。
CUR, BASE = 70.0, 60.0          # net 总量
REV_C, REV_B = 100.0, 80.0
EXP_C, EXP_B = 30.0, 20.0
# 比率场景自洽:AVG == num/den(200/20=10、160/20=8;shift-share 后
# cur_total 取加权率,须与 hop0 的 AVG 同值)
AVG_C, AVG_B = 10.0, 8.0
NUMDEN_C = [["East", 120.0, 10.0], ["West", 80.0, 10.0]]
NUMDEN_B = [["East", 100.0, 10.0], ["West", 60.0, 10.0]]
REGION_C, REGION_B = [["East", 45.0], ["West", 25.0]], [["East", 36.0], ["West", 24.0]]
CHANNEL_C = [["Online", 60.0], ["Store", 10.0]]
CHANNEL_B = [["Online", 20.0], ["Store", 40.0]]


def _model() -> SemanticModel:
    return SemanticModel(
        name="m",
        datasets=[SemanticDataset(
            name="sales", source="sales",
            fields=[
                SemanticField(name="amount", expression="sales.amount", semantic_role="measure"),
                SemanticField(name="cost", expression="sales.cost", semantic_role="measure"),
                SemanticField(name="region", expression="sales.region", semantic_role="dimension"),
                SemanticField(name="channel", expression="sales.channel", semantic_role="dimension"),
                SemanticField(name="day", expression="sales.day", datatype="Date", is_time=True),
            ],
        )],
        metrics=[
            SemanticMetric(name="revenue", expression="SUM(sales.amount)", datasets=["sales"]),
            SemanticMetric(name="expense", expression="SUM(sales.cost)", datasets=["sales"]),
            # 字段遮蔽靶:度量名 "cost" 与字段 "cost" 同名
            SemanticMetric(name="cost", expression="SUM(sales.cost)", datasets=["sales"]),
            SemanticMetric(name="net", expression="revenue - expense", datasets=["sales"],
                           metric_type="derived"),
            SemanticMetric(name="profit", expression="revenue - cost", datasets=["sales"],
                           metric_type="derived"),
            SemanticMetric(name="avg_amount", expression="AVG(sales.amount)", datasets=["sales"]),
        ],
    )


class _SL:
    """最小语义层桩:引擎只用 .model()。"""

    def __init__(self, model: SemanticModel) -> None:
        self._model = model

    def model(self) -> SemanticModel:
        return self._model


def _n_select_cols(sql: str) -> int:
    """SELECT 列表顶层列数(本测试模型的聚合表达式内无逗号)。"""
    head = sql.split("FROM", 1)[0]
    inner = head.split("SELECT", 1)[1]
    return len([p for p in inner.split(",") if p.strip()])


class FakeRunner:
    """记录型 runner:按 SQL 形状回罐头行。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, sql: str, datasource: str) -> tuple[list[str], list[list[Any]]]:
        self.calls.append(sql)
        cur = ">= '2024-02-01'" in sql
        if "__num" in sql:  # 比率 hop(分子/分母双列)
            rows = [list(r) for r in (NUMDEN_C if cur else NUMDEN_B)]
            return ["region", "__num", "__den"], rows
        if "GROUP BY sales.region" in sql:
            return ["region", "net"], [list(r) for r in (REGION_C if cur else REGION_B)]
        if "GROUP BY sales.channel" in sql:
            return ["channel", "net"], [list(r) for r in (CHANNEL_C if cur else CHANNEL_B)]
        n = _n_select_cols(sql)
        if n >= 3:  # 树组合查询 net/revenue/expense
            return (["net", "revenue", "expense"],
                    [[CUR, REV_C, EXP_C]] if cur else [[BASE, REV_B, EXP_B]])
        if n == 2:
            if "AVG(" in sql:  # 树组合查询 avg_amount/revenue
                return ["avg_amount", "revenue"], [[AVG_C, REV_C]] if cur else [[AVG_B, REV_B]]
            return ["profit", "revenue"], [[CUR, REV_C]] if cur else [[BASE, REV_B]]
        if "AVG(" in sql:
            return ["avg_amount"], [[AVG_C if cur else AVG_B]]
        if " - " in sql:
            return ["net"], [[CUR if cur else BASE]]
        return ["revenue"], [[REV_C if cur else REV_B]]


def _req(**kw: Any) -> AnalysisRequest:
    base: dict[str, Any] = {
        "question": "net 这个月为什么变化?",
        "lang": "zh",
        "datasource": "demo",
        "dialect": "sqlite",
        "matched": ["sales"],
        "metric": "net",
        "dimensions": ["region"],
        # 单日窗口:基期 = 2024-01-31(runner 靠当前期字面量区分)
        "time_context": "2024-02-01 ~ 2024-02-01",
    }
    base.update(kw)
    return AnalysisRequest(**base)


def _engine(runner: FakeRunner, **limits: Any) -> AnalysisEngine:
    return AnalysisEngine(_SL(_model()), runner, AnalysisLimits(**limits))


class TestAdditiveTree:
    """加性链:hop 序列 + 树值 + 残差恒等式精确。"""

    async def test_hops_sql_and_table(self):
        runner = FakeRunner()
        out = await _engine(runner).run(_req())
        assert out is not None
        # 6 跳:hop0 双期(overall)→ hop1 双期(probe)→ 树双期(driver_tree)
        purposes = [q["purpose"] for q in out.evidence_queries]
        assert purposes == ["overall", "overall", "probe", "probe", "driver_tree", "driver_tree"]
        assert [q.get("period") for q in out.evidence_queries] == [
            "current", "base", "current", "base", "current", "base"]
        # SQL 形状:hop0 无 GROUP BY;hop1 按主拆维分组
        assert "GROUP BY" not in runner.calls[0] and " - " in runner.calls[0]
        assert "GROUP BY sales.region" in runner.calls[2]
        # 贡献表:East +9 / West +1 → 0.9 / 0.1
        assert [r["dim"] for r in out.table] == ["East", "West"]
        assert out.table[0]["contribution"] == 0.9
        assert out.table[1]["contribution"] == 0.1
        assert out.total_delta == 10.0
        assert out.primary_dim == "region"
        assert out.partial is False
        assert out.degraded == []

    async def test_tree_values_and_exact_residual(self):
        runner = FakeRunner()
        out = await _engine(runner).run(_req())
        tree = out.tree
        assert tree is not None
        assert tree["name"] == "net" and tree["op"] == "-" and tree["decomposable"] is True
        # 根:hop0 口径(不是树查询自己的口径),值来自整体对比
        assert tree["value_source"] == "hop0"
        assert (tree["current"], tree["base"], tree["delta"]) == (CUR, BASE, 10.0)
        kids = {k["name"]: k for k in tree["children"]}
        assert (kids["revenue"]["current"], kids["revenue"]["base"]) == (REV_C, REV_B)
        assert (kids["expense"]["current"], kids["expense"]["base"]) == (EXP_C, EXP_B)
        # 减法带符号:Δnet 10 == Δrevenue 20 − Δexpense 10 → 恒等式精确
        assert tree["residual"]["exact"] is True
        assert tree["residual"]["reason"] == "identity"
        assert tree["residual"]["value"] == 0.0


class TestRatioTree:
    """比率链:num/den 跳形状 + 树不可分解时如实标注。"""

    async def test_ratio_hop_shape_and_effects(self):
        runner = FakeRunner()
        out = await _engine(runner).run(_req(metric="avg_amount"))
        assert out is not None and out.is_ratio is True
        # 比率 hop:分子/分母双列(不是裸聚合)
        assert "__num" in runner.calls[2] and "__den" in runner.calls[2]
        effects = out.effects
        assert effects is not None
        # shift-share 三效应之和 == ΔR(恒等式,数学层已单测,这里查接线)
        assert (effects["within"] + effects["composition"] + effects["interaction"]
                == pytest.approx(effects["delta"]))
        # 加权率 200/20 = 10(与 hop0 的 AVG 一致,两个口径不打架)
        assert out.cur_total == pytest.approx(10.0)

    async def test_tree_non_decomposable_and_unresolvable_child(self):
        runner = FakeRunner()
        out = await _engine(runner).run(_req(metric="avg_amount"))
        tree = out.tree
        assert tree is not None
        assert tree["kind"] == "ratio" and tree["decomposable"] is False
        assert tree["residual"] == {"value": None, "exact": False, "reason": "non_decomposable"}
        # 子节点标 informational(只报值,不声称贡献)
        assert all(k.get("informational") for k in tree["children"])
        # 分母 COUNT(...) 无同签名度量 → 预检剔除 + 记账(不静默)
        reasons = [d["reason"] for d in out.degraded]
        assert any(r.startswith("unresolvable:COUNT(") for r in reasons)
        assert out.partial is True
        # 分子 SUM(...) 经签名解析为 revenue,值在树上
        by_name = {k["candidate"]: k for k in tree["children"]}
        num_node = next(k for c, k in by_name.items() if c.startswith("SUM("))
        assert num_node["executed"] is True and num_node["current"] == REV_C
        # 根值仍走 hop0 口径
        assert tree["value_source"] == "hop0" and tree["current"] == AVG_C


class TestFieldShadowing:
    """裸度量名与声明字段同名 → 编译器字段优先,预检必须剔除。"""

    async def test_shadowed_component_dropped_and_accounted(self):
        runner = FakeRunner()
        out = await _engine(runner).run(_req(metric="profit"))
        assert out is not None
        tree = out.tree
        assert tree is not None
        reasons = [d["reason"] for d in out.degraded]
        assert "field_shadowed:cost" in reasons
        # cost 子节点无值 → 残差理由如实为组件不可得(而不是 0 拼恒等式)
        assert tree["residual"]["reason"] == "component_unavailable"
        assert tree["residual"]["exact"] is False
        cost_node = next(k for k in tree["children"] if k["name"] == "cost")
        assert cost_node["executed"] is False
        # 组合查询只含未遮蔽的度量(2 列)
        tree_query = [c for c in runner.calls if "GROUP BY" not in c]
        assert _n_select_cols(tree_query[-1]) == 2
        assert "SUM(sales.cost)" in tree_query[-1]  # 仍在 net 内联里,但只有 2 列


class TestDimensionProbe:
    """维度预选:Σ|Δ| 最大者居首;探测结果复用为 hop1。"""

    async def test_reorder_and_cache_reuse(self):
        runner = FakeRunner()
        out = await _engine(runner).run(
            _req(dimensions=["region", "channel"], depth=2))
        assert out is not None
        # channel 信号 70 > region 10 → 重排
        assert out.primary_dim == "channel"
        assert out.dimensions[0] == "channel"
        # 探测 4 条(两维双期),hop1 复用探测结果**零重复查询**;下钻 2 条
        purposes = [q["purpose"] for q in out.evidence_queries]
        assert purposes.count("probe") == 4
        assert purposes.count("overall") == 2
        assert purposes.count("drilldown") == 2
        assert purposes.count("driver_tree") == 2  # 树阶段照跑(预算内)
        assert len(runner.calls) == 10
        # 下钻:主拆维 top 项("Online")过滤,按次维(region)分解
        assert out.drilldown is not None
        assert out.drilldown["dimension"] == "region"
        drill = [q for q in out.evidence_queries if q["purpose"] == "drilldown"]
        assert all(q["filter"] == "Online" for q in drill)


class TestBudgetAndDegradation:
    """预算截断:树阶段宁缺勿错,骨架树 + 记账。"""

    async def test_query_budget_exceeded_keeps_skeleton(self):
        runner = FakeRunner()
        out = await _engine(runner, max_queries=5).run(_req())
        assert out is not None
        # 老路径 4 跳照跑(预算只控新阶段);树 2 跳被拦
        assert len(runner.calls) == 4
        assert [q["purpose"] for q in out.evidence_queries] == [
            "overall", "overall", "probe", "probe"]
        assert {"stage": "driver_tree", "reason": "query_budget_exceeded"} in out.degraded
        assert out.partial is True
        # 骨架仍在:根有 hop0 值,子节点未执行
        tree = out.tree
        assert tree is not None
        assert tree["value_source"] == "hop0" and tree["current"] == CUR
        assert all(k["executed"] is False for k in tree["children"])

    async def test_no_hops_returns_none(self):
        runner = FakeRunner()
        assert await _engine(runner).run(_req(metric="不存在的度量")) is None
        assert await _engine(runner).run(_req(dimensions=[])) is None
        assert runner.calls == []

    async def test_driver_tree_gate_off(self):
        runner = FakeRunner()
        out = await _engine(runner, driver_tree=False).run(_req())
        assert out is not None and out.tree is None
        assert len(runner.calls) == 4  # 只有老路径


class TestAnalysisPayload:
    """payload 形状(纯函数):kind 判定 / evidence / partial。"""

    def test_kind_matrix(self):
        chart = {"type": "waterfall"}
        tree = {"name": "net"}
        table = [{"dim": "East"}]
        combined = AnalysisOutcome(metric="net", baseline="prev_period",
                                   table=table, tree=tree, evidence_queries=[],
                                   hops=[{"hop": 0}])
        p = analysis_payload(combined, question="q", chart=chart,
                             baseline_label="上期", datasource="demo")
        assert p["kind"] == "combined" and p["charts"] == [chart]
        # v2(B8):版本无条件前进 —— 描述的是生产者 schema,不由内容决定
        # (缺席容忍才是兼容机制;v1 形状的 payload 逐键仍是这里的样子)。
        assert p["version"] == 2 and p["metric_kind"] == "additive"
        assert p["labels"]["baseline_label"] == "上期"
        assert p["partial"] is False and p["evidence"]["truncated"] is False

        tree_only = AnalysisOutcome(metric="net", baseline="share", tree=tree,
                                    hops=[{"hop": 0}])
        assert analysis_payload(tree_only, question="", chart=None,
                                baseline_label="", datasource="d")["kind"] == "driver_tree"

        plain = AnalysisOutcome(metric="net", baseline="share", table=table,
                                hops=[{"hop": 0}])
        assert analysis_payload(plain, question="", chart=None,
                                baseline_label="", datasource="d")["kind"] == "attribution"

    def test_evidence_truncation_and_partial_flag(self):
        out = AnalysisOutcome(
            metric="net", baseline="prev_period", table=[{"dim": "E"}],
            evidence_queries=[{"id": 1, "sql": "SELECT 1", "truncated": True}],
            degraded=[{"stage": "driver_tree", "reason": "components_truncated:2"}],
            partial=True, hops=[{"hop": 0}], is_ratio=True,
        )
        p = analysis_payload(out, question="q" * 100, chart=None,
                             baseline_label="", datasource="demo")
        assert p["evidence"]["truncated"] is True
        assert p["partial"] is True and p["metric_kind"] == "ratio"
        assert p["labels"]["question"] == "q" * 60  # 标题截断
        assert p["evidence"]["queries"][0]["sql"] == "SELECT 1"


class _DbTypedRunner(FakeRunner):
    """同 FakeRunner,但按驱动原生类型回行:数字格 → ``Decimal``,一个分组
    键 → ``date``(日期维度;MySQL/PG 都是这么交回 ``DECIMAL`` / ``DATE``)。"""

    async def __call__(self, sql: str, datasource: str) -> tuple[list[str], list[list[Any]]]:
        cols, rows = await super().__call__(sql, datasource)
        return cols, [
            [
                Decimal(str(c)) if isinstance(c, float)
                else (date(2024, 2, 1) if c == "East" else c)
                for c in row
            ]
            for row in rows
        ]


class TestEvidenceJsonSafe:
    """DB 原生类型(Decimal/date)不得掐断交付段 —— 2026-10-05 回归门。

    线上根因:MySQL 适配器把 ``Decimal``/``date`` 原样交回,证据行原样进
    ``state.analysis`` → summary → ``SessionStore.save_session`` 的
    ``json.dumps`` 抛 TypeError,生成器死在 ``done`` 事件之前 —— 用户看到
    「流中断」,答案既不送达也不落库(SSE 侧本有 ``default=str`` 兜底,
    但落库先炸,轮不到它)。

    门打在**出口**:payload 必须零 ``default=`` 可 dumps;保真度一并钉死
    (Decimal → 数字、date → ISO,而不是 ``"Decimal('45')"`` 字符串)。
    """

    async def test_payload_dumps_without_default_and_keeps_fidelity(self):
        out = await _engine(_DbTypedRunner()).run(_req())
        assert out is not None
        payload = analysis_payload(
            out, question="net 为什么变化?", chart=None,
            baseline_label="上期", datasource="demo",
        )
        json.dumps(payload)  # 契约:出口即 JSON,不得抛、不得依赖 default=
        json.dumps(out.hops)  # 跳记录同样进 state(attribution_hops),一并钉

        cells = [c for q in payload["evidence"]["queries"] for r in q["rows"] for c in r]
        assert cells  # 证据行非空(否则本门空转)
        assert not any(isinstance(c, (Decimal, date)) for c in cells)
        assert any(isinstance(c, float) for c in cells)  # Decimal → 数字
        # 日期维度的标签同样不能带 date 对象进 payload(表/树/系列同源)
        assert "2024-02-01" in [r["dim"] for r in payload["table"]]

    def test_record_rows_normalizes_cells(self):
        rows = [[Decimal("1.5"), date(2024, 1, 31), None, True, "East"]]
        assert _record_rows(rows, 10) == [[1.5, "2024-01-31", None, True, "East"]]
        assert _record_rows([[1], [2], [3]], 2) == [[1], [2]]  # 截断仍在


class TestPeriodDegradation:
    """要了 yoy/环比却拿不到两个可比窗口 → 不静默降级(2026-10-05 线上
    回归:静默降 share 后查询无时间过滤,「1997 vs 1996」拿全量数据算,
    结论数字对不上口径且无处可见)。"""

    async def test_missing_time_context_degrades_loudly(self):
        out = await _engine(FakeRunner()).run(_req(time_context="", baseline="yoy"))
        assert out is not None
        assert {"stage": "period", "reason": "no_time_context"} in out.degraded
        assert out.baseline == "share"   # 降级仍发生,但记账了
        assert out.partial is True

    async def test_unparsable_time_context_reason(self):
        out = await _engine(FakeRunner()).run(
            _req(time_context="某年某月", baseline="yoy"))
        assert out is not None
        assert {"stage": "period", "reason": "unparsable_time_context"} in out.degraded

    async def test_share_baseline_is_not_a_degradation(self):
        """share 本来就没有基期 —— 不记账(降级与设计语义分得开)。"""
        out = await _engine(FakeRunner()).run(_req(time_context="", baseline="share"))
        assert out is not None
        assert not any(d.get("stage") == "period" for d in out.degraded)


class _GapRunner(FakeRunner):
    """区域行之和 != 总量(缺桶):West 基期被抬到 25 → Δ=0,ΣΔ=9 ≠ 10。

    只截**加性路径**的区域查询:比率 hop 的 SQL 同样含
    ``GROUP BY sales.region``,但它以 ``__num`` 双列标志(形状
    ``[region, __num, __den]`` 三列)——放行给 super。缺 ``__num``
    守卫的失败模式是**静默**的:比率路径拿到两列行后退化成空表,
    测试 3 的「不检查」断言照样通过(空洞通过,评审实测)——
    这道守卫正是让该分支被真跑到的那道闸。
    (驱动器树的 SQL 不含 GROUP BY:engine.py 的 ``_compile_one`` 对
    含 GROUP BY 的编译结果直接返回 None,天然不入此分支。)
    """

    async def __call__(self, sql: str, datasource: str):
        if "__num" not in sql and "GROUP BY sales.region" in sql:
            self.calls.append(sql)
            cur = ">= '2024-02-01'" in sql
            rows = [["East", 45.0], ["West", 25.0]]
            if not cur:
                rows = [["East", 36.0], ["West", 25.0]]
            return ["region", "net"], [list(r) for r in rows]
        return await super().__call__(sql, datasource)


class TestConservation:
    """聚合守恒挂点(设计 §2.2):缺口落 degraded 且 partial 置真;比率不查。"""

    async def test_gap_lands_in_degraded_and_partial(self):
        out = await _engine(_GapRunner()).run(_req())
        assert out is not None
        assert any(d.get("stage") == "conservation" for d in out.degraded)
        assert out.partial is True

    async def test_consistent_sum_no_degraded(self):
        out = await _engine(FakeRunner()).run(_req())
        assert all(d.get("stage") != "conservation" for d in out.degraded)
        assert out.partial is False

    async def test_ratio_path_not_checked(self):
        out = await _engine(_GapRunner()).run(_req(metric="avg_amount"))
        assert all(d.get("stage") != "conservation" for d in out.degraded)


class _FocusRunner(FakeRunner):
    """像真库一样分工:含 focus 等值谓词的 hop1 **只**回被点名段。

    focus="East" 时 hop1 的 SQL 带 ``sales.region = 'East'``(见
    ``compile_hop`` 的谓词拼装)→ 单段口径;hop0/探测/树不含该谓词 →
    全体口径(委托 super)。真库语义:WHERE 命中一行东区行,不是把
    全体行改名成 East。
    """

    async def __call__(self, sql: str, datasource: str):
        if "sales.region = 'East'" in sql:
            self.calls.append(sql)
            cur = ">= '2024-02-01'" in sql
            rows = [["East", 45.0]] if cur else [["East", 36.0]]
            return ["region", "net"], [list(r) for r in rows]
        return await super().__call__(sql, datasource)


class TestConservationFocusScope:
    """focus 在场跳过守恒自查(最终评审 Important 1)。

    focus 下两口径不可比:hop1 表只覆盖被点名的段(ΣΔ=9),``total_delta``
    出自未过滤的 hop0(全体 Δ=10)——跨口径相减必留缺口,自查会把每次
    focus 归因都误报成「decomposition sum != total_delta」+ partial 置真
    (设计 §2.3「不假警」的反面)。

    判别力:删掉 ``engine.py`` 守卫里的 ``and not focus``,本用例必红
    —— ``_FocusRunner`` 的分段回行会让缺口真实发生(旧的 `_GapRunner`
    用例回全体口径,删守卫也照样守恒成立,抓不住这条回归)。
    """

    async def test_focus_skips_cross_scope_check(self):
        out = await _engine(_FocusRunner()).run(_req(focus="East"))
        assert out is not None
        # 段口径本身正确:表只有 East,delta 9(45 − 36)
        assert [r["dim"] for r in out.table] == ["East"]
        assert out.table[0]["delta"] == 9.0
        assert out.total_delta == 10.0  # 全体口径(hop0),口径不同才对
        assert all(d.get("stage") != "conservation" for d in out.degraded)
        assert out.partial is False
