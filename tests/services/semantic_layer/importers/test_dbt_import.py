"""dbt Semantic Layer → IR:映射、manifest 解析、有损构造的报告。"""

from __future__ import annotations

from pathlib import Path

import pytest

from trove.services.semantic_layer.importers.base import SemanticImportError
from trove.services.semantic_layer.importers.dbt import parse_dbt

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "semantic_import"
PROJECT = FIXTURES / "dbt_project"
MANIFEST = PROJECT / "target" / "manifest.json"


def _by_name(imported, name):
    return next(m for m in imported.metrics if m.name == name)


def _dataset(imported, name):
    return next(d for d in imported.datasets if d.name == name)


def test_clean_project_with_manifest_maps_everything():
    imported = parse_dbt(PROJECT, manifest_path=MANIFEST)

    assert imported.counts() == {
        "datasets": 2, "fields": 3, "metrics": 4, "relationships": 1,
    }
    assert imported.warnings == []
    assert imported.unsupported == []

    orders = _dataset(imported, "orders")
    assert orders.source == "analytics.orders"
    assert orders.primary_key == ["order_id"]
    assert orders.unique_keys == []
    assert orders.description == "One row per order"
    assert [(f.name, f.expression, f.is_time) for f in orders.fields] == [
        ("order_date", "ordered_at", True),
        ("status", "status", False),
    ]
    assert _dataset(imported, "customers").source == "analytics.customers"


def test_relationship_from_foreign_entity_resolves_primary_key():
    (rel,) = parse_dbt(PROJECT, manifest_path=MANIFEST).relationships
    assert rel.name == "orders_to_customers"
    assert (rel.from_dataset, rel.to_dataset) == ("orders", "customers")
    assert rel.from_columns == ["customer_id"]
    assert rel.to_columns == ["customer_id"]
    assert rel.cardinality == "1:N"


def test_simple_ratio_and_derived_metrics_are_dataset_qualified():
    imported = parse_dbt(PROJECT, manifest_path=MANIFEST)

    revenue = _by_name(imported, "revenue")
    assert revenue.expression == "SUM(orders.amount)"
    assert revenue.datasets == ["orders"]
    assert revenue.description == "Total revenue"
    assert revenue.metric_type == ""

    ratio = _by_name(imported, "average_order_value")
    assert ratio.metric_type == "ratio"
    assert ratio.datasets == ["orders"]
    assert ratio.expression == "(SUM(orders.amount)) / NULLIF(COUNT(orders.order_id), 0)"

    derived = _by_name(imported, "revenue_minus_ten")
    assert derived.metric_type == "derived"
    assert derived.datasets == ["orders"]
    assert "SUM(orders.amount)" in derived.expression
    assert derived.expression.endswith("- 10")


def test_without_manifest_falls_back_to_ref_name_with_warning():
    imported = parse_dbt(PROJECT)

    assert sorted(d.source for d in imported.datasets) == ["customers", "orders"]
    assert {w.code for w in imported.warnings} == {"relation_unresolved"}
    assert sorted(w.target for w in imported.warnings) == ["customers", "orders"]


def test_missing_manifest_path_raises(tmp_path):
    with pytest.raises(SemanticImportError, match="manifest 不存在"):
        parse_dbt(PROJECT, manifest_path=tmp_path / "nope.json")


def test_lossy_project_reports_warnings_and_unsupported():
    imported = parse_dbt(FIXTURES / "dbt_lossy")

    assert imported.counts() == {
        "datasets": 1, "fields": 1, "metrics": 0, "relationships": 0,
    }
    assert _dataset(imported, "sessions").primary_key == ["session_id"]

    warning_codes = {w.code for w in imported.warnings}
    assert {"jinja_unresolved", "relation_unresolved", "measure_unresolved"} <= warning_codes

    unsupported_codes = {u.code for u in imported.unsupported}
    assert {
        "saved_queries_unsupported",
        "measure_filter_unsupported",
        "measure_agg_unsupported",
        "metric_type_unsupported",
    } <= unsupported_codes
    metric_skips = [u for u in imported.unsupported if u.code == "metric_type_unsupported"]
    assert {u.target for u in metric_skips} == {"gmv_cumulative", "conversion_rate"}


def test_already_qualified_expression_is_not_double_prefixed(tmp_path):
    (tmp_path / "models.yml").write_text(
        "semantic_models:\n"
        "  - name: orders\n"
        "    model: orders\n"
        "    measures:\n"
        "      - name: revenue_measure\n"
        "        agg: sum\n"
        "        expr: orders.amount\n"
        "metrics:\n"
        "  - name: revenue\n"
        "    type: simple\n"
        "    type_params:\n"
        "      measure: revenue_measure\n",
        encoding="utf-8")

    (metric,) = parse_dbt(tmp_path).metrics
    assert metric.expression == "SUM(orders.amount)"


def test_empty_directory_raises(tmp_path):
    with pytest.raises(SemanticImportError, match="没有找到"):
        parse_dbt(tmp_path)
