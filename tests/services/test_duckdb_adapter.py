"""DuckDB adapter tests — unit (fake driver) + real in-memory integration."""

import importlib

import pytest

from trove.core.errors import DatasourceError, SQLExecutionError
from trove.services.datasource.adapters import duckdb as duckdb_module
from trove.services.datasource.adapters.duckdb import DuckDBAdapter


class FakeRelation:
    def __init__(self, description, rows):
        self.description = description
        self._rows = rows

    def fetchall(self):
        return self._rows


class FakeConn:
    def __init__(self, scripted=None):
        # scripted: list of FakeRelation returned per execute() call
        self._scripted = list(scripted or [])
        self.executed = []
        self.closed = False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        return self._scripted.pop(0) if self._scripted else FakeRelation([], [])

    def close(self):
        self.closed = True


class FakeDriver:
    def __init__(self, conn=None):
        self.conn = conn or FakeConn()
        self.connect_kwargs = None

    def connect(self, **kwargs):
        self.connect_kwargs = kwargs
        return self.conn


def make_adapter(monkeypatch, driver=None, config=None):
    driver = driver or FakeDriver()
    monkeypatch.setattr(duckdb_module, "_get_driver", lambda: driver)
    adapter = DuckDBAdapter(name="test", config=config or {"path": ":memory:"})
    return adapter, driver


class TestDuckDBAdapter:
    async def test_connect_memory(self, monkeypatch):
        adapter, driver = make_adapter(monkeypatch)
        await adapter.connect()
        assert adapter.is_connected
        assert driver.connect_kwargs["database"] == ":memory:"

    async def test_connect_file_path(self, monkeypatch):
        adapter, driver = make_adapter(monkeypatch, config={"path": "/tmp/d.duckdb"})
        await adapter.connect()
        assert driver.connect_kwargs["database"] == "/tmp/d.duckdb"

    async def test_connect_failure_wraps_datasource_error(self, monkeypatch):
        class BadDriver:
            def connect(self, **kwargs):
                raise OSError("cannot open file")

        monkeypatch.setattr(duckdb_module, "_get_driver", lambda: BadDriver())
        adapter = DuckDBAdapter(name="test", config={"path": ":memory:"})
        with pytest.raises(DatasourceError):
            await adapter.connect()

    async def test_execute_returns_query_result(self, monkeypatch):
        conn = FakeConn([FakeRelation(
            [("id", None), ("name", None)], [(1, "a"), (2, "b")],
        )])
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(conn))
        await adapter.connect()

        result = await adapter.execute("SELECT id, name FROM t")
        assert result.columns == ["id", "name"]
        assert result.rows == [[1, "a"], [2, "b"]]
        assert result.row_count == 2
        assert conn.executed[0][0] == "SELECT id, name FROM t"

    async def test_execute_error_wraps(self, monkeypatch):
        class ExplodingConn:
            def execute(self, sql, params=None):
                raise RuntimeError("no such table")

        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(ExplodingConn()))
        await adapter.connect()

        with pytest.raises(SQLExecutionError) as exc_info:
            await adapter.execute("SELECT * FROM ghost")
        assert "no such table" in str(exc_info.value)

    async def test_execute_before_connect_raises(self, monkeypatch):
        adapter, _ = make_adapter(monkeypatch)
        with pytest.raises(SQLExecutionError):
            await adapter.execute("SELECT 1")

    async def test_get_schema_uses_pragma_table_info(self, monkeypatch):
        # executed: duckdb_tables() → count(*) → PRAGMA table_info
        conn = FakeConn([
            FakeRelation([("table_name",)], [("students",)]),
            FakeRelation([("count",)], [(5,)]),
            FakeRelation(
                [("cid",), ("name",), ("type",), ("notnull",), ("dflt",), ("pk",)],
                [(0, "id", "INTEGER", True, None, True), (1, "name", "VARCHAR", False, None, False)],
            ),
        ])
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(conn))
        await adapter.connect()

        schema = await adapter.get_schema()
        assert len(schema.tables) == 1
        table = schema.tables[0]
        assert table.name == "students"
        assert table.row_count_estimate == 5

        id_col = next(c for c in table.columns if c.name == "id")
        assert id_col.primary_key is True
        assert id_col.nullable is False

        name_col = next(c for c in table.columns if c.name == "name")
        assert name_col.nullable is True
        assert name_col.primary_key is False

        executed = " ".join(sql for sql, _ in conn.executed)
        assert "PRAGMA table_info" in executed

    async def test_an_exactly_counted_empty_table_stays_zero(self, monkeypatch):
        """精确计数为 0 时**保持 0** —— 这是本方言与另外几个的分别。

        §14.12 记的是「四个适配器把不知道写成了 0」;读代码之后不成立的是
        duckdb 这一条:它跑的是 ``SELECT COUNT(*)``(精确值),``COUNT(*)``
        不会返回 NULL,0 就是真值「这张表是空的」。把真值抹成 ``None`` 是另一种
        谎 —— 而且为了一个不会发生的 NULL 去改,代价是让这条路径多一个可能抛
        异常的分支(``int(None)``),它跑在查询主链路上,不能失败。

        真正把 0 当「没有依据」的是**画像层**(``positive_int``):它不知道、
        也不该知道这个值是精确的还是统计估算的,判据只有「正数才算依据」。
        代价是一条打在空表上的查询被加 LIMIT —— 方向安全。

        这是**特征测试**:写下来时它就是绿的,作用是钉住这条刻意的非对称,
        免得下一个人照着「0 不是依据」把它顺手改掉。
        """
        def one_pass():
            # duckdb_tables() → count(*) → PRAGMA table_info,一轮一次架构内省
            return [
                FakeRelation([("table_name",)], [("empty_t",)]),
                FakeRelation([("count",)], [(0,)]),
                FakeRelation(
                    [("cid",), ("name",), ("type",), ("notnull",), ("dflt",), ("pk",)],
                    [(0, "id", "INTEGER", True, None, False)],
                ),
            ]

        conn = FakeConn(one_pass() + one_pass())
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(conn))
        await adapter.connect()

        table = (await adapter.get_schema()).tables[0]
        assert table.row_count_estimate == 0  # 真值:这张表就是空的

        # table_profiles 的基线实现复用 get_schema → 再走一轮
        profiles = await adapter.table_profiles()
        assert profiles["empty_t"].row_count is None  # 画像层:不作为依据

    async def test_get_capabilities(self, monkeypatch):
        adapter, _ = make_adapter(monkeypatch)
        caps = await adapter.get_capabilities()
        assert caps.dialect == "duckdb"
        assert caps.supports_cte is True
        assert caps.supports_window_functions is True
        assert caps.supports_transactions is True
        assert caps.supports_json_type is True

    async def test_dialect_static(self):
        assert DuckDBAdapter.dialect() == "duckdb"

    async def test_missing_driver_hint(self, monkeypatch):
        def _missing():
            raise DatasourceError(
                message="duckdb is not installed — run `uv sync --extra duckdb`",
                datasource="",
            )

        monkeypatch.setattr(duckdb_module, "_get_driver", _missing)
        adapter = DuckDBAdapter(name="test", config={"path": ":memory:"})
        with pytest.raises(DatasourceError) as exc_info:
            await adapter.connect()
        assert "uv sync --extra duckdb" in str(exc_info.value)

    async def test_disconnect_closes(self, monkeypatch):
        conn = FakeConn()
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(conn))
        await adapter.connect()
        await adapter.disconnect()
        assert not adapter.is_connected
        assert conn.closed is True


# ── Real in-memory integration (skipped when the driver extra is missing) ──


@pytest.mark.skipif(
    importlib.util.find_spec("duckdb") is None,
    reason="duckdb not installed — run `uv sync --extra duckdb`",
)
class TestDuckDBIntegration:
    async def test_full_lifecycle_in_memory(self):
        adapter = DuckDBAdapter(name="mem", config={"path": ":memory:"})
        await adapter.connect()
        try:
            await adapter.execute("CREATE TABLE students (id INTEGER PRIMARY KEY, name VARCHAR NOT NULL)")
            await adapter.execute("INSERT INTO students VALUES (1, 'Alice'), (2, 'Bob')")

            result = await adapter.execute("SELECT * FROM students ORDER BY id")
            assert result.row_count == 2
            assert result.columns == ["id", "name"]

            schema = await adapter.get_schema()
            table = next(t for t in schema.tables if t.name == "students")
            id_col = next(c for c in table.columns if c.name == "id")
            assert id_col.primary_key is True
            assert table.row_count_estimate == 2
        finally:
            await adapter.disconnect()

    async def test_file_persistence_roundtrip(self, tmp_path):
        path = tmp_path / "data.duckdb"
        adapter = DuckDBAdapter(name="file", config={"path": str(path)})
        await adapter.connect()
        await adapter.execute("CREATE TABLE t (v INTEGER)")
        await adapter.execute("INSERT INTO t VALUES (42)")
        await adapter.disconnect()

        adapter2 = DuckDBAdapter(name="file2", config={"path": str(path)})
        await adapter2.connect()
        try:
            result = await adapter2.execute("SELECT v FROM t")
            assert result.rows == [[42]]
        finally:
            await adapter2.disconnect()
