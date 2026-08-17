"""Statistical profiling tests — stats into schema_notes (AskData 式 profiling)."""

from trove.services.kb.profiling import (
    detect_shape,
    merge_into_stats,
    probe_stats,
)


class TestDetectShape:
    def test_numeric(self):
        assert detect_shape(["12", "-3.5", "1,234", "0"]) == "numeric"

    def test_json(self):
        assert detect_shape(
            ['{"a": 1}', '{"b": 2}', "plain", "plain"]
        ) == "json"

    def test_composite(self):
        """≥1/4 样本含分隔符 → 复合值字段。"""
        assert detect_shape(["12;45", "A,B", "single", "single", "single"]) == "composite"

    def test_all_caps(self):
        assert detect_shape(["POPLATEK TYDNE", "POPLATEK MESICNE"]) == "all_caps"

    def test_capital(self):
        assert detect_shape(["Benesov", "Los Angeles"]) == "capital"

    def test_text(self):
        assert detect_shape(["hello", "world foo"]) == "text"

    def test_empty_returns_none(self):
        assert detect_shape([]) is None
        assert detect_shape([None, None]) is None

    def test_all_null_with_quote_sample(self):
        """混合脏值:单个数字不触发 numeric(要求全部匹配)。"""
        assert detect_shape(["123", "abc"]) == "text"


class TestMergeIntoStats:
    def test_fills_stats_non_destructive(self):
        notes = {"tables": [{"name": "account", "columns": [
            {"name": "frequency", "description": "x", "enums": []},
        ]}]}
        profiled = {"account": {
            "row_count": 100,
            "columns": {"frequency": {
                "null_ratio": 0.0, "distinct": 4, "shape": "all_caps",
            }},
        }}
        merged = merge_into_stats(notes, profiled)
        assert merged["tables"][0]["row_count"] == 100
        assert merged["tables"][0]["columns"][0]["stats"] == {
            "null_ratio": 0.0, "distinct": 4, "shape": "all_caps",
        }
        # 调用方结构不被修改
        assert "stats" not in notes["tables"][0]["columns"][0]
        assert "row_count" not in notes["tables"][0]

    def test_existing_stats_kept(self):
        notes = {"tables": [{"name": "t", "columns": [
            {"name": "c", "stats": {"distinct": 7}},
        ]}]}
        merged = merge_into_stats(notes, {"t": {"columns": {"c": {"distinct": 9}}}})
        assert merged["tables"][0]["columns"][0]["stats"] == {"distinct": 7}

    def test_overwrite_replaces(self):
        notes = {"tables": [{"name": "t", "columns": [
            {"name": "c", "stats": {"distinct": 7}},
        ]}]}
        merged = merge_into_stats(
            notes, {"t": {"columns": {"c": {"distinct": 9}}}}, overwrite=True,
        )
        assert merged["tables"][0]["columns"][0]["stats"] == {"distinct": 9}

    def test_unknown_table_or_column_ignored(self):
        merged = merge_into_stats({"tables": []}, {"nope": {"columns": {}}})
        assert merged["tables"] == []
        merged = merge_into_stats(
            {"tables": [{"name": "t", "columns": []}]},
            {"t": {"columns": {"nope": {"distinct": 1}}}},
        )
        assert merged["tables"][0]["columns"] == []

    def test_none_values_dropped(self):
        """null_ratio None(空表)不写入 stats。"""
        notes = {"tables": [{"name": "t", "columns": [{"name": "c"}]}]}
        merged = merge_into_stats(
            notes, {"t": {"columns": {"c": {"null_ratio": None, "distinct": 0}}}},
        )
        assert merged["tables"][0]["columns"][0]["stats"] == {"distinct": 0}


class TestProbeStats:
    async def test_probes_numeric_and_text_columns(self, sqlite_registry):
        schema = await sqlite_registry.get_schema()
        profiled = await probe_stats(sqlite_registry, schema, max_rows=100)
        students = profiled["students"]
        assert students["row_count"] == 5
        cols = students["columns"]
        assert cols["id"]["null_ratio"] == 0.0
        assert cols["id"]["distinct"] == 5
        assert cols["grade"]["min"] == 75
        assert cols["grade"]["max"] == 99
        assert cols["name"]["shape"] == "capital"   # Alice/Bob/Carol/...
        assert cols["name"]["min_len"] == 3         # Bob
        assert cols["name"]["max_len"] == 5         # Alice/Carol
        assert cols["county"]["shape"] == "capital"  # Alameda/Orange/Los Angeles
        assert cols["county"]["distinct"] == 3

    async def test_row_count_guard_skips_large_tables(self, sqlite_registry):
        schema = await sqlite_registry.get_schema()
        profiled = await probe_stats(sqlite_registry, schema, max_rows=1)
        assert profiled == {}  # 行数估计 5 > 护栏 1 → 整表跳过
