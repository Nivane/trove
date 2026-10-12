"""对象图投影单测(设计 §4.5,FR-GRAPH-1/2)。

钉住四件事:

1. **投影 ≡ 声明**:每个对象指回数据集声明、每条边指回 relationship 声明,
   未声明 entity 的数据集以 ``declared: false`` 的「表」节点出现;
2. **读侧回退**(设计 §4.1/§4.2):display_name → 数据集 name、natural_keys →
   primary_key、边 display_name → 列连接串(``from_columns → to_columns``,
   与前端 ``columnFallback`` 逐字节同款);层级 display_name **无回退**;
3. **fan_out 是布尔派生值**:与编译器 ``_classify_edges`` 同输入同判定
   (行为表 × 一致性断言),``unknown_cardinality`` 不折进这个字段;
4. **确定性**:None/空模型 → 空图不抛;同输入逐字节同输出(三连
   PYTHONHASHSEED 子进程钉死,与 decompose 同款)。
"""
from __future__ import annotations

import dataclasses

import pytest

from trove.services.semantic_layer.models import (
    SemanticModel,
    SemanticRelationship,
)
from trove.services.semantic_layer.object_graph import (
    _link_display_name,
    _link_fan_out,
    build_object_graph,
)
from trove.services.semantic_layer.ossie import parse_ossie

GRAPH_YAML = """
semantic_model:
  - name: demo
    datasets:
      - name: loan
        source: loan
        primary_key: [loan_id]
        entity:
          display_name: 贷款
          natural_keys: [loan_id]
          aliases: [借款, loan]
        fields:
          - name: loan_id
            expression: {dialects: [{dialect: ANSI_SQL, expression: loan_id}]}
          - name: amount
            expression: {dialects: [{dialect: ANSI_SQL, expression: amount}]}
      - name: customer
        source: customer
        primary_key: [customer_id]
        fields:
          - name: customer_id
            expression: {dialects: [{dialect: ANSI_SQL, expression: customer_id}]}
          - name: region
            expression: {dialects: [{dialect: ANSI_SQL, expression: region}]}
    relationships:
      - name: loan_to_customer
        from: loan
        to: customer
        from_columns: [customer_id]
        to_columns: [customer_id]
        display_name: 属于
      - name: customer_to_loan
        from: customer
        to: loan
        from_columns: [loan_id]
        to_columns: [loan_id]
    metrics:
      - name: total_loan
        expression: {dialects: [{dialect: ANSI_SQL, expression: SUM(loan.amount)}]}
      - name: customer_count
        expression: {dialects: [{dialect: ANSI_SQL, expression: COUNT(customer.customer_id)}]}
    hierarchies:
      - name: region_hierarchy
        display_name: 地区层级
        dataset: customer
        levels: [region, customer_id]
      - name: bare_hierarchy
        dataset: customer
        levels: [customer_id]
"""


def _model():
    return parse_ossie(GRAPH_YAML, preferred_dialect="sqlite")


# ── 投影 ≡ 声明 ─────────────────────────────────────────────


def test_objects_point_back_to_dataset_declarations():
    model = _model()
    graph = build_object_graph(model)
    assert [o["dataset"] for o in graph["objects"]] == [d.name for d in model.datasets]


def test_links_point_back_to_relationship_declarations():
    model = _model()
    graph = build_object_graph(model)
    assert [l["name"] for l in graph["links"]] == [r.name for r in model.relationships]


def test_undeclared_dataset_is_a_declared_false_table_node():
    """未声明 entity 的数据集**不消失** —— 图的完整性让覆盖率缺口可见。"""
    graph = build_object_graph(_model())
    by_name = {o["dataset"]: o for o in graph["objects"]}
    assert by_name["loan"]["declared"] is True
    assert by_name["customer"]["declared"] is False


def test_entity_without_display_name_still_declared():
    """只声明 natural_keys 也算已声明;display_name 走回退(声明层不填默认)。"""
    model = _model()
    entity = dataclasses.replace(model.datasets[0].entity, display_name="")
    ds = dataclasses.replace(model.datasets[0], entity=entity)
    graph = build_object_graph(dataclasses.replace(model, datasets=[ds]))
    obj = graph["objects"][0]
    assert obj["declared"] is True
    assert obj["display_name"] == "loan"


def test_declared_display_name_wins_over_dataset_name():
    graph = build_object_graph(_model())
    by_name = {o["dataset"]: o for o in graph["objects"]}
    assert by_name["loan"]["display_name"] == "贷款"
    assert by_name["customer"]["display_name"] == "customer"


def test_natural_keys_fall_back_to_primary_key():
    graph = build_object_graph(_model())
    by_name = {o["dataset"]: o for o in graph["objects"]}
    assert by_name["loan"]["natural_keys"] == ["loan_id"]  # 声明值
    assert by_name["customer"]["natural_keys"] == ["customer_id"]  # primary_key 回退


def test_aliases_passthrough_declared_only():
    graph = build_object_graph(_model())
    by_name = {o["dataset"]: o for o in graph["objects"]}
    assert by_name["loan"]["aliases"] == ["借款", "loan"]
    assert by_name["customer"]["aliases"] == []


def test_metrics_anchored_by_dataset():
    graph = build_object_graph(_model())
    by_name = {o["dataset"]: o for o in graph["objects"]}
    assert by_name["loan"]["metrics"] == ["total_loan"]
    assert by_name["customer"]["metrics"] == ["customer_count"]


def test_field_count_counts_declared_fields():
    graph = build_object_graph(_model())
    by_name = {o["dataset"]: o for o in graph["objects"]}
    assert by_name["loan"]["field_count"] == 2
    assert by_name["customer"]["field_count"] == 2


# ── 读侧回退(设计 §4.1/§4.2)────────────────────────────────


def test_link_display_name_declared_wins():
    graph = build_object_graph(_model())
    by_name = {l["name"]: l for l in graph["links"]}
    assert by_name["loan_to_customer"]["display_name"] == "属于"


def test_link_display_name_fallback_is_column_arrow():
    """与前端 ``ontologyLayout.linkFallback`` 逐字节同款:``a, b → c, d``。"""
    graph = build_object_graph(_model())
    by_name = {l["name"]: l for l in graph["links"]}
    assert by_name["customer_to_loan"]["display_name"] == "loan_id → loan_id"


def test_link_display_name_fallback_without_columns_uses_dataset_names():
    """无列对(解析器产不出,但投影是纯函数)→ ``from → to`` 数据集名。"""
    r = SemanticRelationship(name="e", from_="loan", to="customer")
    assert _link_display_name(r) == "loan → customer"


def test_multi_column_fallback_joins_with_comma():
    r = SemanticRelationship(
        name="e", from_="a", to="b",
        from_columns=["k1", "k2"], to_columns=["j1", "j2"])
    assert _link_display_name(r) == "k1, k2 → j1, j2"


def test_link_payload_carries_key_pairs_and_cardinality():
    graph = build_object_graph(_model())
    link = next(l for l in graph["links"] if l["name"] == "loan_to_customer")
    assert link["from"] == "loan" and link["to"] == "customer"
    assert link["from_columns"] == ["customer_id"]
    assert link["to_columns"] == ["customer_id"]
    assert link["cardinality"] == ""


def test_hierarchy_passthrough_without_invented_fallback():
    """层级 display_name 没有文档化的回退:声明原样,未声明保持空串。"""
    graph = build_object_graph(_model())
    by_name = {h["name"]: h for h in graph["hierarchies"]}
    assert by_name["region_hierarchy"] == {
        "name": "region_hierarchy", "display_name": "地区层级",
        "dataset": "customer", "levels": ["region", "customer_id"],
    }
    assert by_name["bare_hierarchy"]["display_name"] == ""


# ── fan_out:布尔派生值,判定与编译器同源 ─────────────────────

#: 五种 M:N 拼写 × 三种豁免串。期望值 = 编译器 ``_classify_edges`` 的
#: fan_out 判定(非 dedup 一律 True;``bridge:<dataset>`` 豁免未实现,
#: 保守 True)。
_MN_SPELLINGS = ["M:N", "N:M", "M2M", "MANY-TO-MANY", "many_to_many"]
_EXEMPTIONS = [("", True), ("dedup", False), ("bridge:orders", True)]


@pytest.mark.parametrize("spelling", _MN_SPELLINGS)
@pytest.mark.parametrize("exemption,expected", _EXEMPTIONS,
                         ids=["none", "dedup", "bridge"])
def test_fan_out_boolean_behavior_table(spelling, exemption, expected):
    r = SemanticRelationship(
        name="e", from_="a", to="b",
        cardinality=spelling, fan_out=exemption)
    result = _link_fan_out(r)
    assert result is expected
    assert isinstance(result, bool)  # 不是原始串


@pytest.mark.parametrize("cardinality", ["", "N:1", "1:N", "1:1", None])
@pytest.mark.parametrize("exemption", ["", "dedup", "bridge:ds"])
def test_non_mn_edges_never_flagged(cardinality, exemption):
    """非 M:N(含空基数/未声明)一律 False —— 豁免串只在 M:N 边上有意义。"""
    r = SemanticRelationship(
        name="e", from_="a", to="b",
        cardinality=cardinality or "", fan_out=exemption)
    assert _link_fan_out(r) is False


def _compiler_verdict(cardinality: str, fan_out: str) -> bool:
    """单边过编译器 ``_classify_edges`` → fan_out 判定(与 :205 同款归一)。

    边构造照 ``JoinResolver._declared_edges``:cardinality 大写、fan_out
    ``strip().lower()``;判定用真实 ``JoinResolver`` 实例(模型只有它自己
    的声明,不注入唯一键推断)。
    """
    from trove.services.semantic_layer.compiler import JoinEdge, JoinResolver

    edge = JoinEdge(
        "a", "b", "k", "j", declared=True,
        cardinality=(cardinality or "").upper(),
        fan_out=(fan_out or "").strip().lower(),
    )
    fan_out_flag, _unknown, _dedup = JoinResolver()._classify_edges([edge])
    return fan_out_flag


@pytest.mark.parametrize("spelling", _MN_SPELLINGS)
@pytest.mark.parametrize("exemption", ["", "dedup", "bridge:orders",
                                       " DEDUP "])
def test_fan_out_agrees_with_compiler_classify_edges(spelling, exemption):
    """语义一致性门:投影布尔 == 编译器 fan_out 判定(同输入)。

    两处判定漂移的后果是同一份声明在对象图页看着安全、在编译期被拒
    (或反过来)—— 这个断言就是那道防漂移门。``" DEDUP "`` 行同时钉住
    两侧的 ``strip().lower()`` 归一。
    """
    r = SemanticRelationship(
        name="e", from_="a", to="b",
        cardinality=spelling, fan_out=exemption)
    assert _link_fan_out(r) is _compiler_verdict(spelling, exemption)


def test_fan_out_agrees_on_declared_edges_end_to_end():
    """YAML 声明的四条边:模型 → 编译器边构造 → 判定,与投影逐条相同。"""
    from trove.services.semantic_layer.compiler import JoinResolver

    yaml_text = """
semantic_model:
  - name: demo
    datasets:
      - {name: a, primary_key: [id]}
      - {name: b, primary_key: [id]}
    relationships:
      - {name: r_dedup, from: a, to: b, from_columns: [bid], to_columns: [id],
         cardinality: M:N, fan_out: dedup}
      - {name: r_bridge, from: a, to: b, from_columns: [x], to_columns: [id],
         cardinality: M:N, fan_out: bridge:orders}
      - {name: r_bare, from: a, to: b, from_columns: [y], to_columns: [id],
         cardinality: MANY-TO-MANY}
      - {name: r_safe, from: a, to: b, from_columns: [z], to_columns: [id],
         cardinality: N:1}
"""
    model = parse_ossie(yaml_text, preferred_dialect="sqlite")
    resolver = JoinResolver(model)
    edges = resolver._declared_edges({"a", "b"})
    by_pair: dict[tuple[str, str], list] = {}
    for e in edges:
        by_pair.setdefault((e.from_, e.to), []).append(e)
    graph = build_object_graph(model)
    link_by_rel = {l["name"]: l for l in graph["links"]}
    rels = {r.name: r for r in model.relationships}
    for name, r in rels.items():
        # 同一条关系的所有列对共用一个豁免串,聚合判定 = 单边判定
        pair_edges = by_pair[(r.from_, r.to)]
        pair_edges = [e for e in pair_edges
                      if e.from_column in r.from_columns]
        flag, _, _ = resolver._classify_edges(pair_edges)
        assert link_by_rel[name]["fan_out"] is flag, name
    # 顺带钉住行为表在真实解析链上的期望值
    assert link_by_rel["r_dedup"]["fan_out"] is False
    assert link_by_rel["r_bridge"]["fan_out"] is True
    assert link_by_rel["r_bare"]["fan_out"] is True
    assert link_by_rel["r_safe"]["fan_out"] is False


def test_unknown_cardinality_is_not_folded_into_fan_out():
    """空基数 = 「无从判定」(另一回事),不是「会倍增」—— 不折进 fan_out。"""
    from trove.services.semantic_layer.compiler import JoinEdge, JoinResolver

    edge = JoinEdge("a", "b", "k", "j", declared=True, cardinality="", fan_out="")
    fan_out_flag, unknown_card, _ = JoinResolver()._classify_edges([edge])
    assert unknown_card is True  # 编译器看到了「判不了」
    assert fan_out_flag is False
    r = SemanticRelationship(name="e", from_="a", to="b")
    assert _link_fan_out(r) is False  # 投影不发明风险


def test_fan_out_field_never_carries_the_raw_string():
    graph = build_object_graph(_model())
    for link in graph["links"]:
        assert isinstance(link["fan_out"], bool)


# ── 空图与确定性 ────────────────────────────────────────────


def test_none_model_yields_empty_graph_without_throwing():
    assert build_object_graph(None) == {
        "objects": [], "links": [], "hierarchies": []}


def test_empty_model_yields_empty_graph():
    assert build_object_graph(SemanticModel(name="empty")) == {
        "objects": [], "links": [], "hierarchies": []}


def test_partial_declarations_degrade_to_empty_lists():
    m = SemanticModel(name="m")
    graph = build_object_graph(m)
    assert graph["hierarchies"] == []
    assert graph["links"] == []


class TestDeterminism:
    """同输入逐字节同输出,与 PYTHONHASHSEED 无关(三连子进程钉死)。"""

    _CODE = (
        "import json;"
        "from trove.services.semantic_layer.object_graph "
        "import build_object_graph;"
        "from trove.services.semantic_layer.ossie import parse_ossie;"
        "from trove.services.semantic_layer.models import ("
        " SemanticModel, SemanticDataset, SemanticRelationship, Hierarchy);"
        "m = SemanticModel(name='demo',"
        " datasets=[SemanticDataset(name='b'), SemanticDataset(name='a'),"
        " SemanticDataset(name='c')],"
        " relationships=[SemanticRelationship(name='r2', from_='b', to='a'),"
        " SemanticRelationship(name='r1', from_='a', to='c')],"
        " hierarchies=[Hierarchy(name='h2'), Hierarchy(name='h1')]);"
        "print(json.dumps(build_object_graph(m), ensure_ascii=False,"
        " sort_keys=False))"
    )

    def test_output_stable_across_hash_seeds(self):
        import os
        import subprocess
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parents[3]
        outs = set()
        for seed in ("0", "1", "999"):
            env = dict(os.environ, PYTHONHASHSEED=seed)
            r = subprocess.run(
                [sys.executable, "-c", self._CODE],
                capture_output=True, text=True, env=env,
                cwd=str(root), timeout=120,
            )
            assert r.returncode == 0, r.stderr
            outs.add(r.stdout.strip())
        assert len(outs) == 1, f"跨进程不一致: {outs}"

    def test_declaration_order_is_preserved(self):
        """顺序 = 声明顺序(不排序、不去重)—— 输出是文档的一部分。"""
        graph = build_object_graph(_model())
        assert [o["dataset"] for o in graph["objects"]] == ["loan", "customer"]
        assert [h["name"] for h in graph["hierarchies"]] == [
            "region_hierarchy", "bare_hierarchy"]
