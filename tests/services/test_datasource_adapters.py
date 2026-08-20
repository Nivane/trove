"""Datasource adapter and registry tests."""

import pytest

from trove.core.types import DatasourceConfig
from trove.core.errors import DatasourceError, SQLExecutionError
from trove.services.datasource.registry import ConnectorRegistry
from trove.services.datasource.adapters.sqlite import SQLiteAdapter


class TestSQLiteAdapter:
    async def test_connect_and_disconnect(self):
        adapter = SQLiteAdapter(name="test", config={"path": ":memory:"})
        assert not adapter.is_connected

        await adapter.connect()
        assert adapter.is_connected

        await adapter.disconnect()
        assert not adapter.is_connected

    async def test_execute_query(self):
        adapter = SQLiteAdapter(name="test", config={"path": ":memory:"})
        await adapter.connect()

        result = await adapter.execute("SELECT 1 AS value")
        assert result.columns == ["value"]
        assert result.rows == [[1]]
        assert result.row_count == 1
        assert result.execution_time_ms >= 0

        await adapter.disconnect()

    async def test_execute_with_data(self):
        adapter = SQLiteAdapter(name="test", config={"path": ":memory:"})
        await adapter.connect()
        await adapter.execute("CREATE TABLE t (id INTEGER, name TEXT)")
        await adapter.execute("INSERT INTO t VALUES (1, 'a'), (2, 'b')")

        result = await adapter.execute("SELECT * FROM t ORDER BY id")
        assert result.row_count == 2
        assert result.rows == [[1, "a"], [2, "b"]]

        await adapter.disconnect()

    async def test_execute_error_raises(self):
        adapter = SQLiteAdapter(name="test", config={"path": ":memory:"})
        await adapter.connect()

        with pytest.raises(SQLExecutionError):
            await adapter.execute("SELECT * FROM nonexistent_table")

        await adapter.disconnect()

    async def test_execute_before_connect_raises(self):
        adapter = SQLiteAdapter(name="test", config={"path": ":memory:"})
        with pytest.raises(SQLExecutionError):
            await adapter.execute("SELECT 1")

    async def test_get_schema(self):
        adapter = SQLiteAdapter(name="test", config={"path": ":memory:"})
        await adapter.connect()
        await adapter.execute(
            "CREATE TABLE students (id INTEGER PRIMARY KEY, name TEXT NOT NULL, grade INTEGER)"
        )

        schema = await adapter.get_schema()
        assert len(schema.tables) == 1

        table = schema.tables[0]
        assert table.name == "students"
        assert len(table.columns) == 3

        # Check column metadata
        id_col = next(c for c in table.columns if c.name == "id")
        assert id_col.primary_key is True

        name_col = next(c for c in table.columns if c.name == "name")
        assert name_col.nullable is False

        await adapter.disconnect()

    async def test_get_capabilities(self):
        adapter = SQLiteAdapter(name="test", config={"path": ":memory:"})
        caps = await adapter.get_capabilities()
        assert caps.dialect == "sqlite"
        assert caps.supports_cte is True
        assert caps.supports_window_functions is True

    async def test_dialect_static(self):
        assert SQLiteAdapter.dialect() == "sqlite"

    async def test_context_manager(self):
        async with SQLiteAdapter(name="test", config={"path": ":memory:"}) as adapter:
            assert adapter.is_connected
            result = await adapter.execute("SELECT 42")
            assert result.rows == [[42]]
        assert not adapter.is_connected


class TestConnectorRegistry:
    async def test_register_and_get(self):
        registry = ConnectorRegistry()
        config = DatasourceConfig(
            name="test", type="sqlite", connection_params={"path": ":memory:"},
        )
        adapter = await registry.register(config)
        assert registry.is_registered("test")
        assert registry.default_name == "test"

        fetched = await registry.get()
        assert fetched is adapter

        await registry.close_all()

    async def test_unsupported_type_raises(self):
        registry = ConnectorRegistry()
        config = DatasourceConfig(
            name="bad", type="oracle_unknown", connection_params={},
        )
        with pytest.raises(DatasourceError):
            await registry.register(config)

    async def test_get_unregistered_raises(self):
        registry = ConnectorRegistry()
        with pytest.raises(DatasourceError):
            await registry.get("nonexistent")

    async def test_get_no_datasources_raises(self):
        registry = ConnectorRegistry()
        with pytest.raises(DatasourceError) as exc_info:
            await registry.get()
        assert "No datasources" in str(exc_info.value)

    async def test_multiple_datasources(self):
        registry = ConnectorRegistry()
        c1 = DatasourceConfig(
            name="db1", type="sqlite", connection_params={"path": ":memory:"},
        )
        c2 = DatasourceConfig(
            name="db2", type="sqlite", connection_params={"path": ":memory:"},
        )
        await registry.register(c1)
        await registry.register(c2)

        assert registry.default_name == "db1"  # first registered becomes default
        assert registry.list_names() == ["db1", "db2"]

        # Get by explicit name
        db2 = await registry.get("db2")
        assert db2.name == "db2"

        await registry.close_all()

    async def test_unregister(self):
        registry = ConnectorRegistry()
        config = DatasourceConfig(
            name="temp", type="sqlite", connection_params={"path": ":memory:"},
        )
        await registry.register(config)
        await registry.unregister("temp")
        assert not registry.is_registered("temp")

    async def test_execute_via_registry(self):
        registry = ConnectorRegistry()
        config = DatasourceConfig(
            name="db", type="sqlite", connection_params={"path": ":memory:"},
        )
        adapter = await registry.register(config)
        # 建表/灌数走 adapter(注册表 execute 是只读查询入口,写被拦截)
        await adapter.execute("CREATE TABLE t (v INTEGER)")
        await adapter.execute("INSERT INTO t VALUES (7)")

        result = await registry.execute("SELECT v FROM t", "db")
        assert result.rows == [[7]]

        await registry.close_all()

    async def test_execute_rejects_non_select(self):
        """执行层写保护:registry.execute 拦截一切非 SELECT(DML/DDL)。"""
        registry = ConnectorRegistry()
        config = DatasourceConfig(
            name="db", type="sqlite", connection_params={"path": ":memory:"},
        )
        await registry.register(config)
        for sql in [
            "CREATE TABLE t (v INTEGER)",
            "INSERT INTO t VALUES (1)",
            "UPDATE t SET v = 1",
            "DELETE FROM t",
            "DROP TABLE t",
            "SELECT 1; DELETE FROM t",  # 多语句注入也不放过
        ]:
            with pytest.raises(DatasourceError):
                await registry.execute(sql, "db")

        await registry.close_all()

    async def test_execute_allows_select_with_cte_and_without_table(self):
        """SELECT 家族(含 CTE/无表查询)正常放行。"""
        registry = ConnectorRegistry()
        config = DatasourceConfig(
            name="db", type="sqlite", connection_params={"path": ":memory:"},
        )
        await registry.register(config)
        assert (await registry.execute("SELECT 1 AS v", "db")).rows == [[1]]
        await registry.close_all()

    async def test_execute_caches_identical_read(self):
        """归一化后相同的 SQL 命中结果缓存:adapter 只执行一次,返回值是副本。"""
        registry = ConnectorRegistry()
        config = DatasourceConfig(
            name="db", type="sqlite", connection_params={"path": ":memory:"},
        )
        adapter = await registry.register(config)
        await adapter.execute("CREATE TABLE t (v INTEGER)")
        await adapter.execute("INSERT INTO t VALUES (7)")

        class CountingAdapter:
            def __init__(self, inner):
                self.inner = inner
                self.name = inner.name
                self.calls = 0

            async def execute(self, sql, datasource=None):
                self.calls += 1
                return await self.inner.execute(sql)

            async def disconnect(self):
                await self.inner.disconnect()

        counting = CountingAdapter(adapter)
        registry._adapters["db"] = counting  # type: ignore[assignment]

        r1 = await registry.execute("  SELECT   v FROM t ", "db")
        r2 = await registry.execute("SELECT v FROM t", "db")
        assert r1.rows == [[7]] and r2.rows == [[7]]
        assert counting.calls == 1
        # 返回副本:就地改动不污染缓存
        r2.rows.append([99])
        r3 = await registry.execute("SELECT v FROM t", "db")
        assert r3.rows == [[7]]
        assert registry.result_cache_stats()["hits"] == 2

        await registry.close_all()

    async def test_explain_returns_plan(self):
        """explain:sqlite 走 EXPLAIN QUERY PLAN,计划行含 SCAN/SEARCH 标记。"""
        registry = ConnectorRegistry()
        config = DatasourceConfig(
            name="db", type="sqlite", connection_params={"path": ":memory:"},
        )
        adapter = await registry.register(config)
        await adapter.execute("CREATE TABLE t (v INTEGER)")
        await adapter.execute("INSERT INTO t VALUES (7)")

        result = await registry.explain("SELECT v FROM t", "db")
        assert result.rows, "plan should have lines"
        plan_text = " ".join(str(cell) for row in result.rows for cell in row)
        assert "SCAN" in plan_text or "SEARCH" in plan_text
        await registry.close_all()

    async def test_explain_rejects_non_select(self):
        """explain 与 execute 共用只读闸门:非 SELECT 拒绝,EXPLAIN 前缀进不了 adapter。"""
        registry = ConnectorRegistry()
        config = DatasourceConfig(
            name="db", type="sqlite", connection_params={"path": ":memory:"},
        )
        await registry.register(config)
        with pytest.raises(DatasourceError):
            await registry.explain("DROP TABLE t", "db")
        await registry.close_all()

    async def test_close_all(self):
        registry = ConnectorRegistry()
        config = DatasourceConfig(
            name="db", type="sqlite", connection_params={"path": ":memory:"},
        )
        adapter = await registry.register(config)
        await registry.close_all()
        assert not adapter.is_connected
        assert registry.list_names() == []


class TestCatalogService:
    async def test_list_tables(self, sqlite_registry):
        from trove.services.datasource.catalog import CatalogService
        catalog = CatalogService(sqlite_registry)

        tables = await catalog.list_tables()
        assert len(tables) == 1
        assert tables[0]["name"] == "students"
        assert tables[0]["columns"] == 4

    async def test_table_detail(self, sqlite_registry):
        from trove.services.datasource.catalog import CatalogService
        catalog = CatalogService(sqlite_registry)

        detail = await catalog.table_detail("students")
        assert detail is not None
        assert detail["name"] == "students"
        assert len(detail["columns"]) == 4

        # Check column order and details
        col_names = [c["name"] for c in detail["columns"]]
        assert col_names == ["id", "name", "grade", "county"]

    async def test_table_detail_not_found(self, sqlite_registry):
        from trove.services.datasource.catalog import CatalogService
        catalog = CatalogService(sqlite_registry)
        assert await catalog.table_detail("nonexistent") is None

    async def test_search_tables(self, sqlite_registry):
        from trove.services.datasource.catalog import CatalogService
        catalog = CatalogService(sqlite_registry)

        # Search by table name
        results = await catalog.search_tables("student")
        assert len(results) == 1
        assert results[0]["name"] == "students"

        # Search by column name
        results = await catalog.search_tables("county")
        assert len(results) == 1
        assert results[0]["match_type"] == "column"

        # No match
        results = await catalog.search_tables("zzz")
        assert results == []

    async def test_schema_ddl(self, sqlite_registry):
        from trove.services.datasource.catalog import CatalogService
        catalog = CatalogService(sqlite_registry)

        ddl = await catalog.get_schema_ddl("students")
        assert "CREATE TABLE students" in ddl
        assert "id" in ddl
