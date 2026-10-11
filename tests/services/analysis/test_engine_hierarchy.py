"""层级消费 + FR-HIER-4 候选扩展(E7 分析接线):干预点 (b)/(c/d)/(g)。

- **(b) 同层级候选按级别序重排(声明序压 probe 序)**:probe 信号可以让
  自由维度上位,但**不许**把同一条层级倒过来(细级别压到粗级别前面);
  混合场景(一层级 + 一自由维度)probe 行为照旧。
- **(c/d) 下钻目标 = 层级 L+1**:d0 是级别字段 → 下钻改同层级细一级
  (声明序即方向,probe 重排不改变方向);末级/无层级 → 回退 dim_refs[1]
  现状;过滤机制不变(仍按 d0 的 top 项钉等值)。
- **(g) 显示名标注**:行卡 ``dim_display`` + 载荷标签 ``primary_dimension_
  display`` / ``dimensions_display`` —— **无声明 → 键不出现、逐字节不变**。
- **FR-HIER-4**:经已声明链接 1 跳可达数据集的维度字段**追加**为候选
  (不插队、受 max_dimensions 名额约束);fan-out 边排除;未声明链接零变化。

runner 按 SQL 形状回罐头行(罐头数字自洽:各维 cur 之和 = 70、base 之和
= 60,与 hop0 的 net 70/60 一致 → 守恒自查不报缺);周期靠当前窗口起始
字面量 ``>= '2024-02-01'`` 区分(单日窗口,半开区间下基期 SQL 含
``< '2024-02-01'``,所以必须认 ``>=`` 一侧)。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from trove.services.analysis.engine import (
    AnalysisEngine,
    AnalysisLimits,
    analysis_payload,
)
from trove.services.semantic_layer.models import (
    Hierarchy,
    SemanticDataset,
    SemanticField,
    SemanticMetric,
    SemanticModel,
    SemanticRelationship,
)
from tests.services.analysis.test_engine import FakeRunner, _req

# ── 罐头数字:每维 cur 之和 70、base 之和 60(= hop0 net),Δ 之和恒为 10 ──
# 信号 = Σ|Δ|:drill 场景 province 50 > channel 20(不换主拆维);
# 混合场景 channel 70 > city 14(自由维度正常上位)。
PROV_BLOCK = ([["华东", 30.0], ["华北", 40.0]], [["华东", 24.0], ["华北", 36.0]])  # sig 10
CITY_BLOCK = ([["上海", 48.0], ["北京", 22.0]], [["上海", 36.0], ["北京", 24.0]])  # sig 14
PROV_BIG = ([["华东", 60.0], ["华北", 10.0]], [["华东", 30.0], ["华北", 30.0]])    # sig 50
CHANNEL_SMALL = ([["Online", 45.0], ["Store", 25.0]], [["Online", 30.0], ["Store", 30.0]])  # sig 20
CHANNEL_BIG = ([["Online", 60.0], ["Store", 10.0]], [["Online", 20.0], ["Store", 40.0]])    # sig 70
CHANNEL_LEAF = ([["Online", 50.0], ["Store", 20.0]], [["Online", 41.0], ["Store", 19.0]])   # sig 10
GEO_BIG = ([["North", 60.0], ["South", 10.0]], [["North", 30.0], ["South", 30.0]])  # sig 50
GEO_TIE = ([["North", 45.0], ["South", 25.0]], [["North", 35.0], ["South", 25.0]])  # sig 10


def _fields(*spec: tuple[str, str]):
    return [
        SemanticField(name=name, expression=f"sales.{name}", semantic_role=role)
        for name, role in spec
    ] + [SemanticField(name="day", expression="sales.day", datatype="Date", is_time=True)]


def _hier_model(
    *,
    displays: bool = False,
    hierarchies: bool = True,
    relationships: bool = False,
    neighbors: tuple[str, ...] = (),
) -> SemanticModel:
    """sales(层级/显示名/可调)+ 可选 1 跳邻域数据集(FR-HIER-4)。"""
    sales = SemanticDataset(
        name="sales", source="sales",
        fields=_fields(
            ("amount", "measure"), ("cost", "measure"),
            ("province", "dimension"), ("city", "dimension"),
            ("channel", "dimension"),
        ),
    )
    if displays:
        sales.fields[2].enum_display = {"华东": "East China"}
        sales.fields[3].enum_display = {"上海": "Shanghai"}
    datasets = [sales]
    rels: list[SemanticRelationship] = []
    for i, name in enumerate(neighbors, start=1):
        datasets.append(SemanticDataset(
            name=name, source=name,
            fields=[
                SemanticField(name=f"{name}_name", expression=f"{name}.{name}_name",
                              semantic_role="dimension"),
                SemanticField(name="pop", expression=f"{name}.pop",
                              semantic_role="measure"),
                SemanticField(name="updated", expression=f"{name}.updated",
                              datatype="Date", is_time=True),
            ],
        ))
        rels.append(SemanticRelationship(
            name=f"sales_{name}", from_="sales", to=name,
            from_columns=["province"], to_columns=[f"{name}_name"],
            cardinality="1:N",
        ))
    return SemanticModel(
        name="m",
        datasets=datasets,
        metrics=[
            SemanticMetric(name="revenue", expression="SUM(sales.amount)", datasets=["sales"]),
            SemanticMetric(name="expense", expression="SUM(sales.cost)", datasets=["sales"]),
            SemanticMetric(name="net", expression="revenue - expense", datasets=["sales"],
                           metric_type="derived"),
        ],
        hierarchies=([Hierarchy(name="geo", display_name="地理" if displays else "",
                                dataset="sales", levels=["province", "city"])]
                     if hierarchies else []),
        relationships=rels,
    )


class _SL:
    def __init__(self, model: SemanticModel) -> None:
        self._model = model

    def model(self) -> SemanticModel:
        return self._model


class DimRunner(FakeRunner):
    """按 GROUP BY 段里的维度键回罐头行;其余(hop0/树)委托基类。"""

    def __init__(self, dims: dict[str, tuple[list, list]]) -> None:
        super().__init__()
        self.dims = dims

    async def __call__(self, sql: str, datasource: str):
        cur = ">= '2024-02-01'" in sql
        tail = sql.split("GROUP BY", 1)[1] if "GROUP BY" in sql else ""
        for key, (c, b) in self.dims.items():
            if key in tail:
                self.calls.append(sql)
                return [key, "net"], [list(r) for r in (c if cur else b)]
        return await super().__call__(sql, datasource)


def _engine(model: SemanticModel, runner: FakeRunner, **limits: Any) -> AnalysisEngine:
    return AnalysisEngine(_SL(model), runner, AnalysisLimits(**limits))


def _purposes(out) -> list[str]:
    return [q["purpose"] for q in out.evidence_queries]


def _payload(out) -> dict[str, Any]:
    return analysis_payload(out, question="net 这个月为什么变化?", chart=None,
                            baseline_label="上期", datasource="demo")


class TestReorderWithinHierarchy:
    """干预点 b:声明序压 probe 序 —— 只整理同层级成员。"""

    async def test_finer_level_cannot_promote_over_coarser(self):
        """city 信号更大,但 province 是它同层级的上一级 → 提升让路。"""
        runner = DimRunner({"province": PROV_BLOCK, "city": CITY_BLOCK})
        out = await _engine(_hier_model(), runner).run(
            _req(dimensions=["city", "province"]))
        assert out is not None
        # 计划序 city→province 被整理成粗→细;city 信号 14 > province 10
        # 但倒序提升被拦 → 主拆维保持 province(声明序赢)
        assert out.primary_dim == "province"
        assert out.dimensions == ["province", "city"]
        # 探测 4 条(两维双期);hop1 复用 province 自己的探测结果 —— 拦截
        # 不让路成重复查询(6 跳 = 2 overall + 4 probe,无第二次 hop1)
        assert _purposes(out) == ["overall", "overall", "probe", "probe",
                                  "probe", "probe", "driver_tree", "driver_tree"]
        hop1_sql = [c for c in runner.calls if "GROUP BY sales.province" in c]
        # 探测双期 + hop1 复用(不新增查询)
        assert len(hop1_sql) == 2
        assert out.partial is False

    async def test_free_dimension_still_promotes(self):
        """混合场景(一层级 + 一自由维度):自由维度 promote 照旧。"""
        runner = DimRunner({"city": CITY_BLOCK, "channel": CHANNEL_BIG})
        out = await _engine(_hier_model(), runner).run(
            _req(dimensions=["city", "channel"]))
        assert out is not None
        # city 独属层级的组只有一员 → 恒等;channel 信号 70 > 14 → 上位
        assert out.primary_dim == "channel"
        assert out.dimensions == ["channel", "city"]
        assert _purposes(out).count("probe") == 4  # 复用,零重复查询


class TestHierarchyDrill:
    """干预点 c/d:下钻目标 = 同层级 L+1;末级/无层级回退现状。"""

    async def test_drill_goes_to_next_level(self):
        runner = DimRunner({"province": PROV_BIG, "channel": CHANNEL_SMALL,
                            "city": CITY_BLOCK})
        out = await _engine(_hier_model(), runner).run(
            _req(dimensions=["province", "channel"], depth=2))
        assert out is not None
        assert out.primary_dim == "province"  # 50 > 20,不换
        # 下钻目标 = province 的下一级 city(不是 dim_refs[1]=channel)
        assert out.drilldown is not None
        assert out.drilldown["dimension"] == "city"
        drill = [q for q in out.evidence_queries if q["purpose"] == "drilldown"]
        assert len(drill) == 2
        assert all("GROUP BY sales.city" in q["sql"] for q in drill)
        # 过滤机制不变:仍按 d0 的 top 项钉等值(华东 Δ+30 居首)
        assert all(q["filter"] == "华东" for q in drill)
        assert all("sales.province = '华东'" in q["sql"] for q in drill)

    async def test_leaf_level_falls_back_to_second_dim(self):
        runner = DimRunner({"city": CITY_BLOCK, "channel": CHANNEL_LEAF})
        out = await _engine(_hier_model(), runner).run(
            _req(dimensions=["city", "channel"], depth=2))
        assert out is not None
        # city 是末级 → next_level None → 回退 dim_refs[1](现状)
        assert out.primary_dim == "city"
        assert out.drilldown is not None
        assert out.drilldown["dimension"] == "channel"
        drill = [q for q in out.evidence_queries if q["purpose"] == "drilldown"]
        assert all("GROUP BY sales.channel" in q["sql"] for q in drill)
        assert all("sales.city = '上海'" in q["sql"] for q in drill)

    async def test_without_hierarchy_behavior_unchanged(self):
        """红线下限:未声明层级 → 下钻仍是 dim_refs[1],逐字节同现状。"""
        runner = DimRunner({"province": PROV_BIG, "channel": CHANNEL_SMALL})
        out = await _engine(_hier_model(hierarchies=False), runner).run(
            _req(dimensions=["province", "channel"], depth=2))
        assert out is not None
        assert out.primary_dim == "province"
        assert out.drilldown["dimension"] == "channel"
        drill = [q for q in out.evidence_queries if q["purpose"] == "drilldown"]
        assert all("GROUP BY sales.channel" in q["sql"] for q in drill)


class TestRelatedDimCandidates:
    """FR-HIER-4:1 跳可达维度追加候选(不插队、名额受限、fan-out 排除)。"""

    async def test_reachable_dim_appended_and_can_win(self):
        runner = DimRunner({"channel": CHANNEL_BLOCK_TIE, "geo_name": GEO_BIG})
        out = await _engine(_hier_model(neighbors=("geo",)), runner).run(
            _req(dimensions=["channel"]))
        assert out is not None
        # 追加候选过 probe 门:geo_name 信号 50 > channel 10 → 上位
        assert out.primary_dim == "geo_name"
        assert out.dimensions == ["geo_name", "channel"]
        assert "GROUP BY geo.geo_name" in " ".join(runner.calls)
        assert _purposes(out).count("probe") == 4  # channel + geo_name 双期

    async def test_tie_goes_to_planned_dim(self):
        """信号同分 → 既有(计划)维度优先,追加候选不插队。"""
        runner = DimRunner({"channel": CHANNEL_BLOCK_TIE, "geo_name": GEO_TIE})
        out = await _engine(_hier_model(neighbors=("geo",)), runner).run(
            _req(dimensions=["channel"]))
        assert out is not None
        assert out.primary_dim == "channel"
        assert out.dimensions == ["channel"]

    async def test_fan_out_edge_excluded(self):
        """M:N 且未 dedup 的边不产生候选(复用编译器基数守卫判定)。"""
        model = _hier_model(neighbors=("hub",))
        model.relationships[0].cardinality = "M:N"
        runner = DimRunner({"channel": CHANNEL_BLOCK_TIE, "hub_name": GEO_BIG})
        out = await _engine(model, runner).run(_req(dimensions=["channel"]))
        assert out is not None
        assert out.primary_dim == "channel"
        assert out.dimensions == ["channel"]
        # 候选集合只有 channel(1 个)→ 探测预选不跑,形状 = 老路径
        # (hop1 的 probe 双期是既有语义,不是预选)
        assert len(runner.calls) == 6
        assert "hub" not in " ".join(runner.calls)

    async def test_mn_dedup_edge_is_candidate(self):
        """discriminating control:同一 M:N 边加 dedup 豁免后恢复候选。"""
        model = _hier_model(neighbors=("hub",))
        model.relationships[0].cardinality = "M:N"
        model.relationships[0].fan_out = "dedup"
        runner = DimRunner({"channel": CHANNEL_BLOCK_TIE, "hub_name": GEO_BIG})
        out = await _engine(model, runner).run(_req(dimensions=["channel"]))
        assert out is not None
        assert out.primary_dim == "hub_name"
        assert "GROUP BY hub.hub_name" in " ".join(runner.calls)

    async def test_slots_and_declaration_order(self):
        model = _hier_model(neighbors=("geo1", "geo2"))
        dims = {"channel": CHANNEL_VERY_BIG, "geo1_name": GEO_TIE, "geo2_name": GEO_BIG}
        # 名额 2:1 计划维 + 1 追加 → 只取声明序第一个邻域 geo1
        runner = DimRunner(dims)
        out = await _engine(model, runner, max_dimensions=2).run(
            _req(dimensions=["channel"]))
        assert out is not None
        assert _purposes(out).count("probe") == 4
        assert "geo2" not in " ".join(runner.calls)
        assert out.primary_dim == "channel"
        # 名额 3:两个邻域都进 → 6 条探测,顺序 = 声明序(N32)
        runner2 = DimRunner(dims)
        out2 = await _engine(model, runner2, max_dimensions=3).run(
            _req(dimensions=["channel"]))
        assert out2 is not None
        probe_sql = [q["sql"] for q in out2.evidence_queries if q["purpose"] == "probe"]
        assert len(probe_sql) == 6
        groups = [c.split("GROUP BY")[1].strip() for c in probe_sql]
        assert groups[:2] == ["sales.channel", "sales.channel"]
        assert groups[2:4] == ["geo1.geo1_name", "geo1.geo1_name"]
        assert groups[4:6] == ["geo2.geo2_name", "geo2.geo2_name"]

    async def test_undeclared_link_zero_change(self):
        """未声明任何链接 → 零候选,跳序/查询数与老路径逐字节相同。

        (单维时 hop1 仍走 ``_probe_dim`` 的双期探测 —— 那是既有语义:
        purposes 里的 probe 两条是 hop1 本体,不是预选回路的产物。)
        """
        runner = DimRunner({"channel": CHANNEL_BLOCK_TIE})
        out = await _engine(_hier_model(), runner).run(_req(dimensions=["channel"]))
        assert out is not None
        assert out.primary_dim == "channel"
        assert out.dimensions == ["channel"]
        assert _purposes(out) == ["overall", "overall", "probe", "probe",
                                  "driver_tree", "driver_tree"]
        assert len(runner.calls) == 6
        assert all("sales.channel" in c or "GROUP BY" not in c for c in runner.calls)


CHANNEL_BLOCK_TIE = ([["Online", 65.0], ["Store", 5.0]], [["Online", 60.0], ["Store", 0.0]])  # sig 10
CHANNEL_VERY_BIG = ([["Online", 60.0], ["Store", 10.0]], [["Online", 30.0], ["Store", 30.0]])  # sig 50


class TestDisplayAnnotations:
    """干预点 g:显示名标注(有声明才出现);无声明 → 缺席容忍。"""

    async def test_rows_and_labels_annotated(self):
        runner = DimRunner({"province": PROV_BIG, "channel": CHANNEL_SMALL,
                            "city": CITY_BLOCK})
        out = await _engine(_hier_model(displays=True), runner).run(
            _req(dimensions=["province", "channel"], depth=2))
        assert out is not None
        # 行卡:值 → enum_display(只映射声明过的值)
        assert out.table[0]["dim"] == "华东"
        assert out.table[0]["dim_display"] == "East China"
        assert "dim_display" not in out.table[1]  # 华北未声明 → 键不出现
        drill_rows = out.drilldown["table"]
        assert [r.get("dim_display") for r in drill_rows] == ["Shanghai", None]
        # 级别字段 → 层级 display_name(无回退);自由维度无声明 → 原文
        assert out.primary_dim_display == "地理"
        assert out.dimensions_display == ["地理", "channel"]
        payload = _payload(out)
        assert payload["labels"]["primary_dimension"] == "province"
        assert payload["labels"]["primary_dimension_display"] == "地理"
        assert payload["labels"]["dimensions_display"] == ["地理", "channel"]
        assert payload["labels"]["dimensions"] == ["province", "channel"]

    async def test_no_declaration_keys_absent(self):
        """红线下限:无层级/枚举/别名/实体声明 → 键不出现,形状逐字节不变。"""
        runner = DimRunner({"province": PROV_BIG, "channel": CHANNEL_SMALL,
                            "city": CITY_BLOCK})
        out = await _engine(_hier_model(hierarchies=False), runner).run(
            _req(dimensions=["province", "channel"], depth=2))
        assert out is not None
        assert out.primary_dim_display == ""
        assert out.dimensions_display == []
        legacy_keys = {"dim", "base", "current", "delta", "contribution"}
        assert all(set(r) == legacy_keys for r in out.table)
        assert all(set(r) == legacy_keys for r in out.drilldown["table"])
        payload = _payload(out)
        assert set(payload["labels"]) == {
            "question", "baseline", "baseline_label", "primary_dimension", "dimensions"}
        # 整包 JSON 可序列化(契约:出口即 JSON)
        json.dumps(payload, ensure_ascii=False)

    async def test_bare_hierarchy_name_still_absent(self):
        """层级声明了但没有 display_name → 不发明(键仍不出现)。"""
        runner = DimRunner({"province": PROV_BIG, "channel": CHANNEL_SMALL})
        out = await _engine(_hier_model(displays=False), runner).run(
            _req(dimensions=["province", "channel"]))
        assert out is not None
        assert out.primary_dim_display == ""
        assert out.dimensions_display == []
        assert all("dim_display" not in r for r in out.table)


class TestDeterminism:
    """PYTHONHASHSEED 三连子进程:层级重排/追加候选/标注不引入集合迭代序。"""

    _CODE = """
import asyncio, json
from trove.services.analysis.engine import AnalysisEngine, AnalysisLimits, AnalysisRequest, analysis_payload
from trove.services.semantic_layer.models import (Hierarchy, SemanticDataset, SemanticField, SemanticMetric, SemanticModel, SemanticRelationship)

DIMS = {
    "province": ([["华东", 30.0], ["华北", 40.0]], [["华东", 24.0], ["华北", 36.0]]),
    "city": ([["上海", 48.0], ["北京", 22.0]], [["上海", 36.0], ["北京", 24.0]]),
    "channel": ([["Online", 34.0], ["Store", 36.0]], [["Online", 30.0], ["Store", 30.0]]),
    "geo_name": ([["North", 15.0]], [["North", 10.0]]),
}

m = SemanticModel(
    name="m",
    datasets=[
        SemanticDataset(name="sales", source="sales", fields=[
            SemanticField(name="amount", expression="sales.amount", semantic_role="measure"),
            SemanticField(name="cost", expression="sales.cost", semantic_role="measure"),
            SemanticField(name="province", expression="sales.province", semantic_role="dimension",
                          enum_display={"\\u534e\\u4e1c": "East China"}),
            SemanticField(name="city", expression="sales.city", semantic_role="dimension",
                          enum_display={"\\u4e0a\\u6d77": "Shanghai"}),
            SemanticField(name="channel", expression="sales.channel", semantic_role="dimension"),
            SemanticField(name="day", expression="sales.day", datatype="Date", is_time=True),
        ]),
        SemanticDataset(name="geo", source="geo", fields=[
            SemanticField(name="geo_name", expression="geo.geo_name", semantic_role="dimension"),
            SemanticField(name="pop", expression="geo.pop", semantic_role="measure"),
            SemanticField(name="updated", expression="geo.updated", datatype="Date", is_time=True),
        ]),
    ],
    metrics=[
        SemanticMetric(name="revenue", expression="SUM(sales.amount)", datasets=["sales"]),
        SemanticMetric(name="expense", expression="SUM(sales.cost)", datasets=["sales"]),
        SemanticMetric(name="net", expression="revenue - expense", datasets=["sales"], metric_type="derived"),
    ],
    hierarchies=[Hierarchy(name="geo", display_name="\\u5730\\u7406", dataset="sales",
                           levels=["province", "city"])],
    relationships=[SemanticRelationship(name="sales_geo", from_="sales", to="geo",
                                        from_columns=["province"], to_columns=["geo_name"],
                                        cardinality="1:N")],
)

class Runner:
    def __init__(self): self.calls = []
    async def __call__(self, sql, datasource):
        cur = ">= '2024-02-01'" in sql
        for key, (c, b) in DIMS.items():
            if ("GROUP BY sales." + key) in sql or ("GROUP BY geo." + key) in sql:
                return [key, "net"], [list(r) for r in (c if cur else b)]
        return ["net"], [[70.0 if cur else 60.0]]

class SL:
    def model(self): return m

async def main():
    req = AnalysisRequest(question="q", lang="zh", datasource="demo", dialect="sqlite",
                          matched=["sales"], metric="net",
                          dimensions=["city", "province", "channel"], depth=2,
                          time_context="2024-02-01 ~ 2024-02-01")
    engine = AnalysisEngine(SL(), Runner(), AnalysisLimits(max_dimensions=4, driver_tree=False))
    out = await engine.run(req)
    payload = analysis_payload(out, question="q", chart=None, baseline_label="x", datasource="demo")
    print(json.dumps(payload, ensure_ascii=False, sort_keys=False))

asyncio.run(main())
"""

    def test_output_stable_across_hash_seeds(self):
        root = Path(__file__).resolve().parents[3]
        outs = set()
        for seed in ("0", "1", "999"):
            env = dict(os.environ, PYTHONHASHSEED=seed)
            r = subprocess.run(
                [sys.executable, "-c", self._CODE],
                capture_output=True, text=True, env=env,
                cwd=str(root), timeout=180,
            )
            assert r.returncode == 0, r.stderr
            outs.add(r.stdout.strip())
        assert len(outs) == 1, f"跨进程不一致: {outs}"
