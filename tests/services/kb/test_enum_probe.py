"""Enum probing tests — distinct values into KB schema_notes enums."""

from trove.services.kb.enum_probe import (
    merge_into_notes,
    merge_ranges_into_notes,
    probe_date_ranges,
    probe_enums,
)


class TestMergeIntoNotes:
    def test_fills_empty_enums(self):
        notes = {"tables": [{"name": "account", "columns": [
            {"name": "frequency", "description": "x", "enums": []},
        ]}]}
        merged = merge_into_notes(notes, {"account": {"frequency": "A; B; C"}})
        assert merged["tables"][0]["columns"][0]["enums"] == ["A", "B", "C"]

    def test_existing_meanings_kept_and_missing_values_appended(self):
        """已有枚举含义不覆盖,但探测到的缺失取值要补齐(裸值,无含义)。"""
        notes = {"tables": [{"name": "account", "columns": [
            {"name": "frequency", "description": "x", "enums": ["A=周发放"]},
        ]}]}
        merged = merge_into_notes(notes, {"account": {"frequency": "A; X; Y"}})
        assert merged["tables"][0]["columns"][0]["enums"] == ["A=周发放", "X", "Y"]

    def test_known_values_not_duplicated(self):
        """探测值已出现在现有枚举里(A=周发放)→ 不重复追加裸值。"""
        notes = {"tables": [{"name": "account", "columns": [
            {"name": "frequency", "description": "x", "enums": ["A=周发放"]},
        ]}]}
        merged = merge_into_notes(notes, {"account": {"frequency": "A"}})
        assert merged["tables"][0]["columns"][0]["enums"] == ["A=周发放"]

    def test_bird_quoted_enums_parsed_for_known_values(self):
        """BIRD 格式("'A' stands for ...")里的取值也算已知,只补真正缺的。"""
        notes = {"tables": [{"name": "loan", "columns": [
            {"name": "status", "description": "x",
             "enums": ["'A' stands for contract finished, no problems;\n'C' stands for running contract"]},
        ]}]}
        merged = merge_into_notes(notes, {"loan": {"status": "A; B; C; D"}})
        assert merged["tables"][0]["columns"][0]["enums"] == [
            "'A' stands for contract finished, no problems;\n'C' stands for running contract",
            "B", "D",
        ]

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


class TestProbeDateRanges:
    async def _make_events_table(self, sqlite_registry):
        """Events 表:date 类型列 + 高基数文本列存 YYMMDD(模拟 BIRD)。"""
        # 灌数据走 adapter:registry.execute 是只读查询入口(守卫拦截写)
        adapter = await sqlite_registry.get()
        await adapter.execute(
            "CREATE TABLE events ("
            "id INTEGER PRIMARY KEY, "
            "happened_on DATE, "
            "stamp TEXT)")
        await adapter.execute(
            "INSERT INTO events (happened_on, stamp) VALUES "
            "('1993-01-15', '930115'), ('1994-06-01', '940601'), "
            "('1995-03-20', '950320'), ('1996-11-02', '961102'), "
            "('1997-04-09', '970409'), ('1998-12-31', '981231'), "
            "('1992-02-10', '920210'), ('1991-05-22', '910522')")
        # stamp 高基数(>PROBE_LIMIT 20):distinct 路径跳过 → 走 MIN/MAX 回退
        await adapter.execute(
            "INSERT INTO events (happened_on, stamp) "
            "SELECT '1999-01-01', printf('%02d%02d%02d', 90 + n / 100, 1, 1 + n) "
            "FROM (WITH RECURSIVE c(n) AS (SELECT 0 UNION ALL SELECT n + 1 "
            "FROM c WHERE n < 19) SELECT n FROM c)")

    async def test_date_typed_column_gets_min_max_range(self, sqlite_registry):
        await self._make_events_table(sqlite_registry)
        schema = await sqlite_registry.get_schema()
        ranges = await probe_date_ranges(sqlite_registry, schema)
        assert ranges["events"]["happened_on"] == ["1991-05-22", "1999-01-01"]
        assert "stamp" in ranges["events"]  # 高基数文本回退

    async def test_text_column_with_yyyymmdd_fallback(self, sqlite_registry):
        """text 列存日期(dedup > limit)→ distinct 路径跳过 → MIN/MAX 回退。"""
        await self._make_events_table(sqlite_registry)
        schema = await sqlite_registry.get_schema()
        ranges = await probe_date_ranges(sqlite_registry, schema)
        assert ranges["events"]["stamp"] == ["900101", "981231"]

    async def test_low_cardinality_text_not_probed_as_date(self, sqlite_registry):
        """文本列 distinct ≤ limit 时由枚举路径覆盖,不重复探测为日期。"""
        schema = await sqlite_registry.get_schema()
        ranges = await probe_date_ranges(sqlite_registry, schema)
        assert "county" not in {  # county 3 个 distinct → 枚举路径
            c for cols in ranges.values() for c in cols
        }

    async def test_high_cardinality_non_date_text_skipped(self, sqlite_registry):
        """高基数普通文本(非日期格式)的 MIN/MAX 不记入。"""
        adapter = await sqlite_registry.get()
        await adapter.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
        await adapter.execute(
            "INSERT INTO notes (body) VALUES "
            "('alpha'), ('beta'), ('gamma'), ('delta'), ('epsilon'), ('zeta'), "
            "('eta'), ('theta'), ('iota'), ('kappa'), ('lambda'), ('mu')")
        schema = await sqlite_registry.get_schema()
        ranges = await probe_date_ranges(sqlite_registry, schema)
        assert "notes" not in ranges or "body" not in ranges.get("notes", {})


class TestMergeRangesIntoNotes:
    def test_fills_empty_range(self):
        notes = {"tables": [{"name": "loan", "columns": [
            {"name": "date", "description": "x", "enums": []},
        ]}]}
        merged = merge_ranges_into_notes(
            notes, {"loan": {"date": ["930101", "971231"]}})
        assert merged["tables"][0]["columns"][0]["range"] == ["930101", "971231"]
        assert "enums" in merged["tables"][0]["columns"][0]

    def test_existing_range_kept_by_default(self):
        notes = {"tables": [{"name": "loan", "columns": [
            {"name": "date", "description": "x", "enums": [],
             "range": ["900101", "910101"]},
        ]}]}
        merged = merge_ranges_into_notes(
            notes, {"loan": {"date": ["930101", "971231"]}})
        assert merged["tables"][0]["columns"][0]["range"] == ["900101", "910101"]

    def test_overwrite_replaces_range(self):
        notes = {"tables": [{"name": "loan", "columns": [
            {"name": "date", "description": "x", "enums": [],
             "range": ["900101", "910101"]},
        ]}]}
        merged = merge_ranges_into_notes(
            notes, {"loan": {"date": ["930101", "971231"]}}, overwrite=True)
        assert merged["tables"][0]["columns"][0]["range"] == ["930101", "971231"]

    def test_unknown_table_or_column_ignored(self):
        merged = merge_ranges_into_notes(
            {"tables": [{"name": "t", "columns": [{"name": "c"}]}]},
            {"nope": {"c": ["1", "2"]}})
        assert "range" not in merged["tables"][0]["columns"][0]
        merged = merge_ranges_into_notes(
            {"tables": [{"name": "t", "columns": []}]},
            {"t": {"nope": ["1", "2"]}})
        assert merged["tables"][0]["columns"] == []
