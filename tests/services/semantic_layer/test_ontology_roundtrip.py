"""设计 §7 的两条往返断言 —— 独立单测模块(E6 施工单)。

任一环节丢字段先红,这是三块声明(entity / relationship.display_name /
hierarchies)在两条链上的存活门:

1. **投影 ≡ 声明**:``parse → build_object_graph``,每个对象指回数据集、
   每条边指回 relationship,声明值逐个透出(不是抽查);
2. **yaml 圆环**:``yaml → model → _model_to_dict → yaml`` 逐字节一致 ——
   把管理视图重建回规范 OSSIE 文档、再解析、再出视图,字节与视图都不变;
   三块声明原样存活。

外加兼容门:**缺省无输出变化** —— 没有三块声明的存量模型在圆环里产出的
文档**不含**这些键(声明层保持缺席即缺席,不发明空脚手架)。
"""
from __future__ import annotations

import yaml

from trove.services.semantic_layer.manage import _model_to_dict
from trove.services.semantic_layer.object_graph import build_object_graph
from trove.services.semantic_layer.ossie import parse_ossie

RICH_YAML = """
version: 0.2.0.dev0
semantic_model:
  - name: demo
    description: 对象视图往返样例
    datasets:
      - name: loan
        source: loan
        primary_key: [loan_id]
        entity:
          display_name: 贷款
          natural_keys: [loan_id]
          aliases: [借款]
        fields:
          - name: loan_id
            datatype: Integer
            expression: {dialects: [{dialect: ANSI_SQL, expression: loan_id}]}
          - name: amount
            datatype: Real
            expression: {dialects: [{dialect: ANSI_SQL, expression: amount}]}
      - name: customer
        source: customer
        primary_key: [customer_id]
        fields:
          - name: customer_id
            datatype: Integer
            expression: {dialects: [{dialect: ANSI_SQL, expression: customer_id}]}
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
        cardinality: N:1
        fan_out: dedup
    metrics:
      - name: total_loan
        expression: {dialects: [{dialect: ANSI_SQL, expression: SUM(loan.amount)}]}
    hierarchies:
      - name: product_hierarchy
        display_name: 产品层级
        dataset: loan
        levels: [loan_id, amount]
"""

PLAIN_YAML = """
semantic_model:
  - name: demo
    datasets:
      - name: loan
        source: loan
        primary_key: [loan_id]
        fields:
          - name: loan_id
            expression: {dialects: [{dialect: ANSI_SQL, expression: loan_id}]}
    relationships:
      - name: loan_to_loan
        from: loan
        to: loan
        from_columns: [loan_id]
        to_columns: [loan_id]
    metrics:
      - name: total_loan
        expression: {dialects: [{dialect: ANSI_SQL, expression: COUNT(loan.loan_id)}]}
"""


def _parse(text: str):
    return parse_ossie(text, preferred_dialect="sqlite")


def _canonical_doc(view: dict) -> dict:
    """管理视图(扁平)→ 规范 OSSIE 文档。

    与 ``test_mask_declaration.TestMaskRoundTrip._reparse`` 同一手法:只有
    ``expression`` 是扁平的(管理页展示用),回灌时重新包回 ``dialects``;
    三块新声明都是**原样**回灌的 —— 它们正是本模块的被测载荷。
    """

    def _expr(text: str) -> dict:
        return {"dialects": [{"dialect": "ANSI_SQL", "expression": text}]}

    def _ds(ds: dict) -> dict:
        out: dict = {"name": ds["name"], "source": ds["source"],
                     "primary_key": list(ds["primary_key"])}
        if ds.get("entity") is not None:
            out["entity"] = {
                "display_name": ds["entity"]["display_name"],
                "natural_keys": list(ds["entity"]["natural_keys"]),
                "aliases": list(ds["entity"]["aliases"]),
            }
        out["fields"] = [
            {
                "name": f["name"],
                **({"datatype": f["datatype"]} if f.get("datatype") else {}),
                "expression": _expr(f["expression"]),
            }
            for f in ds["fields"]
        ]
        return out

    model: dict = {
        "name": view["name"],
        "datasets": [_ds(ds) for ds in view["datasets"]],
        "relationships": [
            {
                "name": r["name"], "from": r["from"], "to": r["to"],
                "from_columns": list(r["from_columns"]),
                "to_columns": list(r["to_columns"]),
                **({"cardinality": r["cardinality"]} if r["cardinality"] else {}),
                **({"fan_out": r["fan_out"]} if r["fan_out"] else {}),
                **({"display_name": r["display_name"]}
                   if r["display_name"] else {}),
            }
            for r in view["relationships"]
        ],
        "metrics": [
            {"name": m["name"], "expression": _expr(m["expression"])}
            for m in view["metrics"]
        ],
    }
    if view["description"]:
        model["description"] = view["description"]
    if view["hierarchies"]:
        model["hierarchies"] = [
            {
                "name": h["name"],
                **({"display_name": h["display_name"]}
                   if h["display_name"] else {}),
                "dataset": h["dataset"],
                "levels": list(h["levels"]),
            }
            for h in view["hierarchies"]
        ]
    doc: dict = {"semantic_model": [model]}
    if view["version"]:
        doc["version"] = view["version"]
    return doc


def _dump(doc: dict) -> str:
    return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False,
                          default_flow_style=False)


def _circle(text: str) -> tuple[str, dict, dict]:
    """yaml → model → view → yaml;返回 (规范文本, 视图, 模型)。"""
    model = _parse(text)
    view = _model_to_dict(model)
    canonical = _dump(_canonical_doc(view))
    return canonical, view, model


# ── 往返 ①:投影 ≡ 声明 ─────────────────────────────────────


def test_every_object_points_back_to_a_dataset_declaration():
    model = _parse(RICH_YAML)
    graph = build_object_graph(model)
    assert {o["dataset"] for o in graph["objects"]} == {
        d.name for d in model.datasets}
    assert len(graph["objects"]) == len(model.datasets)


def test_every_link_points_back_to_a_relationship_declaration():
    model = _parse(RICH_YAML)
    graph = build_object_graph(model)
    assert {l["name"] for l in graph["links"]} == {
        r.name for r in model.relationships}
    assert len(graph["links"]) == len(model.relationships)


def test_every_object_carries_the_declaration_values():
    """逐声明值透出(不是抽查):declared / 显示名 / 自然键 / 别名。"""
    model = _parse(RICH_YAML)
    graph = build_object_graph(model)
    by_name = {o["dataset"]: o for o in graph["objects"]}
    for d in model.datasets:
        obj = by_name[d.name]
        assert obj["declared"] == (d.entity is not None)
        if d.entity and d.entity.display_name:
            assert obj["display_name"] == d.entity.display_name
        else:
            assert obj["display_name"] == d.name
        assert obj["natural_keys"] == (
            list(d.entity.natural_keys) if d.entity and d.entity.natural_keys
            else list(d.primary_key))
        assert obj["aliases"] == (list(d.entity.aliases) if d.entity else [])


def test_every_link_carries_the_declaration_values():
    from trove.services.semantic_layer.object_graph import (
        _link_display_name,
        _link_fan_out,
    )

    model = _parse(RICH_YAML)
    graph = build_object_graph(model)
    by_name = {l["name"]: l for l in graph["links"]}
    for r in model.relationships:
        link = by_name[r.name]
        assert (link["from"], link["to"]) == (r.from_, r.to)
        assert link["from_columns"] == list(r.from_columns)
        assert link["to_columns"] == list(r.to_columns)
        assert link["cardinality"] == r.cardinality
        assert link["display_name"] == _link_display_name(r)
        assert link["fan_out"] is _link_fan_out(r)


def test_every_hierarchy_carries_the_declaration_values():
    model = _parse(RICH_YAML)
    graph = build_object_graph(model)
    by_name = {h["name"]: h for h in graph["hierarchies"]}
    for h in model.hierarchies:
        assert by_name[h.name] == {
            "name": h.name, "display_name": h.display_name,
            "dataset": h.dataset, "levels": list(h.levels)}


# ── 往返 ②:yaml 圆环,逐字节一致 ────────────────────────────


def test_yaml_circle_is_byte_identical():
    """把规范文本再过一次圆环:产出的 YAML 与第一轮**逐字节**一致。"""
    first_text, first_view, _ = _circle(RICH_YAML)
    second_text, second_view, _ = _circle(first_text)
    assert second_text == first_text
    assert second_view == first_view


def test_yaml_circle_preserves_all_three_declarations():
    first_text, first_view, _ = _circle(RICH_YAML)
    _, second_view, _ = _circle(first_text)
    # 三块声明在视图层逐项相等(任一环节丢字段先红)
    assert [ds["entity"] for ds in second_view["datasets"]] == [
        ds["entity"] for ds in first_view["datasets"]]
    assert [r["display_name"] for r in second_view["relationships"]] == [
        r["display_name"] for r in first_view["relationships"]]
    assert second_view["hierarchies"] == first_view["hierarchies"]
    # 且声明本身有内容(全空会让上面的相等退化成「什么都没测」)
    assert any(ds["entity"] for ds in first_view["datasets"])
    assert any(r["display_name"] for r in first_view["relationships"])
    assert first_view["hierarchies"]


def test_yaml_circle_reparsed_model_equals_declaration_view():
    """未规范的写法(裸 ``cardinality: N:1`` / 多行 expression)也落进同一
    个规范固定点:第一轮视图 == 第二轮视图 == 第三轮视图。"""
    first_text, first_view, _ = _circle(
        RICH_YAML.replace("cardinality: N:1", "cardinality: n:1"))
    _, second_view, _ = _circle(first_text)
    assert second_view == first_view


def test_circle_hits_a_fixed_point_after_one_pass():
    """圆环是幂等的:第一轮规范文本 = 不动点,再走不出新字节。"""
    first_text, _, _ = _circle(RICH_YAML)
    third_text, _, _ = _circle(_circle(first_text)[0])
    assert third_text == first_text


# ── 兼容门:缺省无输出变化 ──────────────────────────────────


def test_absent_declarations_stay_absent_in_the_circle():
    """存量模型(没有三块声明)在圆环里产出的文档**不含**这些键 ——
    「未声明」在声明层保持缺席,不发明空脚手架(空块与缺席不同义)。"""
    canonical, view, _ = _circle(PLAIN_YAML)
    doc = yaml.safe_load(canonical)
    model = doc["semantic_model"][0]
    assert all("entity" not in ds for ds in model["datasets"])
    assert all("display_name" not in r for r in model["relationships"])
    assert "hierarchies" not in model
    # 视图侧同理:entity = null 是三键结构外的显式缺席,不是空声明
    assert all(ds["entity"] is None for ds in view["datasets"])
    assert all(r["display_name"] == "" for r in view["relationships"])
    assert view["hierarchies"] == []


def test_absent_declarations_yield_declared_false_objects():
    graph = build_object_graph(_parse(PLAIN_YAML))
    assert all(o["declared"] is False for o in graph["objects"])
    assert graph["hierarchies"] == []
    # 边仍在(未声明显示名 → 列连接串回退)
    assert graph["links"][0]["display_name"] == "loan_id → loan_id"
    assert graph["links"][0]["fan_out"] is False
