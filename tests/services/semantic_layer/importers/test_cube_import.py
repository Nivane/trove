"""Cube 编译 meta → IR:{CUBE} 消解、measure 类型、join 方向与有损报告。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trove.services.semantic_layer.importers.base import SemanticImportError
from trove.services.semantic_layer.importers.cube import parse_cube

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "semantic_import"
META = FIXTURES / "cube_meta.json"


def _metric(imported, name):
    return next(m for m in imported.metrics if m.name == name)


def _dataset(imported, name):
    return next(d for d in imported.datasets if d.name == name)


def test_compiled_meta_maps_cubes_dimensions_and_measures():
    imported = parse_cube(META)

    assert imported.counts() == {
        "datasets": 3, "fields": 5, "metrics": 4, "relationships": 1,
    }
    assert [d.name for d in imported.datasets] == ["orders", "customers", "campaigns"]
    assert [d.source for d in imported.datasets] == ["orders", "customers", "campaigns"]

    orders = _dataset(imported, "orders")
    assert [(f.name, f.expression, f.datatype, f.is_time) for f in orders.fields] == [
        ("id", "id", "Decimal", False),
        ("status", "status", "String", False),
        ("ordered_at", "ordered_at", "DateTime", True),
    ]


def test_measure_types_become_aggregate_expressions():
    imported = parse_cube(META)

    count = _metric(imported, "count")
    assert count.expression == "COUNT(orders.id)"
    assert count.datasets == ["orders"]

    assert _metric(imported, "revenue").expression == "SUM(orders.amount)"
    assert _metric(imported, "distinct_customers").expression == (
        "COUNT(DISTINCT orders.customer_id)")


def test_colliding_measure_names_get_dataset_prefix():
    imported = parse_cube(META)
    assert [m.name for m in imported.metrics] == [
        "count", "revenue", "distinct_customers", "customers_count",
    ]
    assert _metric(imported, "customers_count").expression == "COUNT(customers.id)"


def test_many_to_one_join_maps_and_duplicate_reverse_edge_collapses():
    (rel,) = parse_cube(META).relationships
    assert rel.name == "orders_to_customers"
    assert (rel.from_dataset, rel.to_dataset) == ("orders", "customers")
    assert rel.from_columns == ["customer_id"]
    assert rel.to_columns == ["id"]
    assert rel.cardinality == "1:N"


def test_lossy_constructs_are_reported_not_silently_dropped():
    imported = parse_cube(META)

    codes = [u.code for u in imported.unsupported]
    assert codes == [
        "cube_view_ignored",
        "cube_segment_ignored",
        "derived_cube_unsupported",
        "measure_type_unsupported",
        "join_many_to_many_unsupported",
    ]
    assert "derived_orders" not in {d.name for d in imported.datasets}
    assert "running_revenue" not in {m.name for m in imported.metrics}
    assert imported.warnings == []


def test_join_direction_is_swapped_for_one_to_many(tmp_path):
    meta = {"cubes": [
        {"name": "customers", "sql_table": "customers",
         "dimensions": [{"name": "customers.id", "sql": "{CUBE}.id", "type": "number"}],
         "joins": [{"name": "orders", "relationship": "one_to_many",
                    "sql": "{CUBE}.id = {orders}.customer_id"}]},
        {"name": "orders", "sql_table": "orders",
         "dimensions": [{"name": "orders.customer_id", "sql": "{CUBE}.customer_id",
                         "type": "number"}]},
    ]}
    path = tmp_path / "meta.json"
    path.write_text(json.dumps(meta), encoding="utf-8")

    (rel,) = parse_cube(path).relationships
    assert (rel.from_dataset, rel.to_dataset) == ("orders", "customers")
    assert rel.from_columns == ["customer_id"]
    assert rel.to_columns == ["id"]


def test_non_equality_join_is_reported_and_skipped(tmp_path):
    meta = {"cubes": [
        {"name": "orders", "sql_table": "orders",
         "joins": [{"name": "customers", "relationship": "many_to_one",
                    "sql": "{CUBE}.id > {customers}.id"}]},
        {"name": "customers", "sql_table": "customers"},
    ]}
    path = tmp_path / "meta.json"
    path.write_text(json.dumps(meta), encoding="utf-8")

    imported = parse_cube(path)
    assert imported.relationships == []
    assert [w.code for w in imported.warnings] == ["join_expr_unparsable"]


def test_bare_cube_list_is_accepted_with_warning(tmp_path):
    path = tmp_path / "meta.json"
    path.write_text(json.dumps([
        {"name": "orders", "sql_table": "orders",
         "dimensions": [{"name": "orders.id", "sql": "{CUBE}.id"}]},
    ]), encoding="utf-8")

    imported = parse_cube(path)
    assert imported.counts()["datasets"] == 1
    assert [w.code for w in imported.warnings] == ["bare_cube_list"]


def test_invalid_input_raises(tmp_path):
    missing = tmp_path / "missing.json"
    with pytest.raises(SemanticImportError, match="路径不存在"):
        parse_cube(missing)

    no_cubes = tmp_path / "no_cubes.json"
    no_cubes.write_text(json.dumps({"views": []}), encoding="utf-8")
    with pytest.raises(SemanticImportError, match="cubes"):
        parse_cube(no_cubes)

    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(SemanticImportError, match="解析失败"):
        parse_cube(bad)
