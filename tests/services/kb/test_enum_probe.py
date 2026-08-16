"""Enum probing tests — distinct values into KB schema_notes enums."""

from trove.services.kb.enum_probe import merge_into_notes, probe_enums


class TestMergeIntoNotes:
    def test_fills_empty_enums(self):
        notes = {"tables": [{"name": "account", "columns": [
            {"name": "frequency", "description": "x", "enums": []},
        ]}]}
        merged = merge_into_notes(notes, {"account": {"frequency": "A; B; C"}})
        assert merged["tables"][0]["columns"][0]["enums"] == ["A", "B", "C"]

    def test_existing_enums_not_clobbered_without_overwrite(self):
        """人工已写的枚举含义(如 A=周发放)是宝贵注释,默认探测不得覆盖。"""
        notes = {"tables": [{"name": "account", "columns": [
            {"name": "frequency", "description": "x", "enums": ["A=周发放"]},
        ]}]}
        merged = merge_into_notes(notes, {"account": {"frequency": "X; Y"}})
        assert merged["tables"][0]["columns"][0]["enums"] == ["A=周发放"]

    def test_overwrite_replaces_existing(self):
        notes = {"tables": [{"name": "account", "columns": [
            {"name": "frequency", "description": "x", "enums": ["A=周发放"]},
        ]}]}
        merged = merge_into_notes(notes, {"account": {"frequency": "X; Y"}}, overwrite=True)
        assert merged["tables"][0]["columns"][0]["enums"] == ["X", "Y"]

    def test_unknown_table_or_column_ignored(self):
        merged = merge_into_notes({"tables": []}, {"nope": {"col": "A"}})
        assert merged["tables"] == []
        merged = merge_into_notes(
            {"tables": [{"name": "t", "columns": []}]}, {"t": {"nope": "A"}},
        )
        assert merged["tables"][0]["columns"] == []


class TestProbeEnums:
    async def test_probes_low_cardinality_text_columns(self, sqlite_registry):
        """文本列 distinct ≤ 上限 → 记入枚举;数值列跳过。"""
        schema = await sqlite_registry.get_schema()
        probed = await probe_enums(sqlite_registry, schema, max_rows=100)
        assert "students" in probed
        assert "name" in probed["students"]           # 5 个姓名 → 枚举
        assert "county" in probed["students"]         # 3 个县 → 枚举
        assert "grade" not in probed["students"]      # INTEGER 不探测
        assert sorted(probed["students"]["name"].split("; ")) == [
            "Alice", "Bob", "Carol", "Dave", "Eve",
        ]

    async def test_high_cardinality_column_skipped(self, sqlite_registry):
        """distinct 超过 limit 的列(高基数)不记入枚举。"""
        schema = await sqlite_registry.get_schema()
        probed = await probe_enums(sqlite_registry, schema, max_rows=100, limit=3)
        # students.name 有 5 个 distinct > limit 3 → 跳过
        assert "name" not in probed["students"]
        assert "county" in probed["students"]  # 3 个 ≤ limit → 保留
