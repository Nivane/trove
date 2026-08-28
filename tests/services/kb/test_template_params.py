"""Parameterized template analysis tests (A1)."""

from __future__ import annotations

from trove.services.kb.template_params import analyze_template, extract_params
from trove.services.semantic_layer.models import (
    SemanticDataset,
    SemanticField,
    SemanticModel,
)


def _model(enum_fields: dict[str, dict[str, str]] | None = None) -> SemanticModel:
    fields = [SemanticField("amount", "amount")]
    if enum_fields:
        for fname, enum in enum_fields.items():
            fields.append(SemanticField(fname, fname, enum_display=enum))
    return SemanticModel(name="m", datasets=[SemanticDataset(name="district", fields=fields)])


_TEMPLATE = (
    "SELECT district.A3, SUM(loan.amount) FROM loan "
    "JOIN district ON loan.district_id = district.district_id "
    "WHERE district.A3 = '{{region}}' AND loan.amount > {{min_amount}} "
    "GROUP BY district.A3 ORDER BY {{sort}} LIMIT {{n}}"
)


class TestExtractParams:
    def test_ordered_dedup(self):
        params = extract_params(_TEMPLATE)
        assert [p["name"] for p in params] == ["region", "min_amount", "sort", "n"]

    def test_no_params(self):
        assert extract_params("SELECT COUNT(*) FROM loan") == []


class TestAnalyzeTemplate:
    def test_types_and_column_resolution(self):
        params = analyze_template(_TEMPLATE, _model())
        by = {p["name"]: p for p in params}
        assert by["region"]["type"] == "dimension"
        assert by["region"]["column"] == "district.A3"
        assert by["min_amount"]["type"] == "number"
        assert by["sort"]["type"] == "keyword"
        assert by["n"]["type"] == "number"

    def test_sample_values_from_semantic_model(self):
        model = _model({"A3": {"East": "East", "West": "West", "North": "North"}})
        params = analyze_template(_TEMPLATE, model)
        by = {p["name"]: p for p in params}
        assert by["region"]["sample_values"] == ["East", "West", "North"]

    def test_no_model_no_samples(self):
        params = analyze_template(_TEMPLATE, None)
        by = {p["name"]: p for p in params}
        assert "sample_values" not in by["region"]
        assert by["region"]["column"] == "district.A3"

    def test_plain_sql_no_params(self):
        assert analyze_template("SELECT COUNT(*) FROM loan", None) == []

    def test_column_resolution_unqualified_fallback(self):
        sql = "SELECT * FROM loan WHERE status = '{{st}}'"
        params = analyze_template(sql, None)
        assert params[0]["type"] == "dimension"
        # 无表限定的 EQ 列 → 裸列名(不炸)
        assert params[0].get("column", "") == "status"
