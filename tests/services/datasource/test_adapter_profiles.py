"""适配器侧的画像契约(执行画像设计 §8.2 B / §9.2)。

设计 §8.2 否掉了「统一查 information_schema」——六个方言差异太大(ClickHouse 走
``system.tables``、Doris 走 ``SHOW PARTITIONS``、SQLite 根本没有),统一 SQL 会退化
成「支持最小的那个」。选的是**逐 adapter 实现 + ``capabilities`` 显式声明**。

这个文件钉住「显式声明」到底是什么意思 —— 它最容易写成「这个值不是 None 就说
支持」,而那会把两件完全不同的事混成一件:

* **适配器没实现这个字段**(下游该换数据源,或者知道这条路走不通)
* **这张表的统计信息没收集**(同一套 SQL,重跑一次 ANALYZE 就好了)

前者是能力问题,后者是数据问题。运维看到的第一句话决定他往哪儿修。
"""

from __future__ import annotations

import pytest

from trove.core.types import (
    Capabilities,
    ColumnInfo,
    SchemaInfo,
    TableInfo,
)
from trove.services.datasource.adapters.base import DatabaseAdapter
from trove.services.datasource.adapters.clickhouse import ClickHouseAdapter
from trove.services.datasource.adapters.doris import DorisAdapter
from trove.services.datasource.adapters.duckdb import DuckDBAdapter
from trove.services.datasource.adapters.mysql import MySQLAdapter
from trove.services.datasource.adapters.postgres import PostgresAdapter
from trove.services.datasource.adapters.sqlite import SQLiteAdapter


def _table(name, rows=None):
    return TableInfo(
        name=name, schema="main",
        columns=[ColumnInfo(name="id", type="INTEGER")],
        row_count_estimate=rows,
    )


class _StubAdapter(DatabaseAdapter):
    """最小可用的适配器:只实现抽象方法,**不覆盖画像**。"""

    profile_capabilities = frozenset({"row_count"})

    def __init__(self, tables=(), name="stub"):
        super().__init__(name, {})
        self._tables = list(tables)
        self.schema_calls = 0

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def execute(self, sql):  # pragma: no cover - 本文件不执行 SQL
        raise NotImplementedError

    async def get_schema(self) -> SchemaInfo:
        self.schema_calls += 1
        return SchemaInfo(tables=self._tables)

    async def get_capabilities(self) -> Capabilities:
        return Capabilities(dialect="postgres")

    @staticmethod
    def dialect() -> str:
        return "postgres"


class _BareAdapter(_StubAdapter):
    """什么都不声明的适配器 —— 基线。"""

    profile_capabilities = frozenset()


# ── 基线实现:复用 get_schema 已经有的事实 ────────────────


class TestBaseContract:
    async def test_default_implementation_reuses_get_schema(self):
        """不做方言查询也能给出画像 —— 行数在 ``get_schema`` 里早就有了。

        六个适配器全都填了 ``row_count_estimate``,所以第 2 档今天就能覆盖全部
        方言,不需要为它新写任何 SQL。
        """
        adapter = _StubAdapter([_table("a", 100), _table("b", 200)])
        profiles = await adapter.table_profiles()
        assert profiles["a"].row_count == 100
        assert profiles["b"].row_count == 200

    async def test_capabilities_are_declared_not_inferred_from_a_value(self):
        """**本文件的核心。** 一张表的统计缺失时,字段是 ``None``,但能力**仍在**。

        「这张表缺统计」与「这个库不支持行数」是两回事。按值反推会把前者报成
        后者,把运维引去换数据源(而真正该做的是重跑一次统计收集)。
        """
        adapter = _StubAdapter([_table("a", 100), _table("b", None)])
        profiles = await adapter.table_profiles()
        assert profiles["b"].row_count is None
        assert "row_count" in profiles["b"].capabilities
        assert "row_count" in profiles["a"].capabilities

    async def test_nothing_is_declared_by_default(self):
        """基类**什么都不承诺** —— ``get_schema`` 是抽象方法,但它对
        ``row_count_estimate`` 没有任何保证(字段默认就是 ``None``)。

        所以「声明」必须由各适配器自己做:基类替它宣布「本库支持行数」,
        第三方适配器就会带着一个谎出去。
        """
        profiles = await _BareAdapter([_table("a", 100)]).table_profiles()
        assert profiles["a"].row_count == 100  # 值照样给,能给就给
        assert profiles["a"].capabilities == frozenset()  # 但不算「已声明支持」

    async def test_zero_is_not_a_row_count(self):
        """0 不是依据(同 ``core.types.positive_int``)。

        存量四个适配器写 ``int(row_count or 0)`` —— 统计没收集时会得到一个 0,
        在画像里与「空表」长得一模一样。当成 0 会让一条扫全表的查询被判成零
        成本,方向恰好是**放宽**。
        """
        profiles = await _StubAdapter([_table("a", 0)]).table_profiles()
        assert profiles["a"].row_count is None


class TestAdapterCapabilityMatrix:
    """六个适配器各自声明什么 —— **矩阵是显式的,不是推出来的**。

    这既是能力矩阵本身,也是防回归:新加适配器忘了声明时,这里会红。
    """

    @pytest.mark.parametrize(
        "adapter_cls",
        [SQLiteAdapter, PostgresAdapter, MySQLAdapter, DorisAdapter,
         ClickHouseAdapter, DuckDBAdapter],
    )
    def test_every_adapter_declares_its_schema_backed_fields(self, adapter_cls):
        caps = adapter_cls.profile_capabilities
        assert "row_count" in caps, (
            f"{adapter_cls.__name__} 的 get_schema 填了 row_count_estimate,"
            "却没有声明 row_count —— 未声明会让调用方以为这个库给不出行数"
        )

    def test_clickhouse_goes_beyond_the_schema_baseline(self):
        """ClickHouse 的 ``system.tables`` 同一次查询里就有 ``total_bytes``。

        这是逐 adapter 实现的**收益**:统一查 information_schema 的路线在
        ClickHouse 上连表都没有,只能退化成「支持最小的那个」(§8.2 A 的否决理由)。
        """
        assert "bytes" in ClickHouseAdapter.profile_capabilities
        assert "bytes" not in MySQLAdapter.profile_capabilities  # 尚未实现


# ── ClickHouse:第一处超出基线的方言实现 ───────────────────


class FakeResult:
    def __init__(self, column_names, result_rows):
        self.column_names = column_names
        self.result_rows = result_rows


class FakeClient:
    def __init__(self, scripted=None):
        self._scripted = list(scripted or [])
        self.queries = []

    def query(self, sql, parameters=None):
        self.queries.append((sql, parameters))
        return self._scripted.pop(0) if self._scripted else FakeResult([], [])

    def close(self):
        self.closed = True


class FakeDriver:
    def __init__(self, client):
        self.client = client

    def get_client(self, **kwargs):
        return self.client


def _ch_adapter(monkeypatch, scripted):
    from trove.services.datasource.adapters import clickhouse as ch_module

    client = FakeClient(scripted)
    monkeypatch.setattr(ch_module, "_get_driver", lambda: FakeDriver(client))
    adapter = ClickHouseAdapter(
        name="ch", config={"host": "h", "database": "testdb"},
    )
    return adapter, client


class TestClickHouseProfile:
    async def test_reports_bytes_alongside_rows(self, monkeypatch):
        adapter, client = _ch_adapter(monkeypatch, [
            FakeResult(["name", "total_rows", "total_bytes"],
                       [("events", 5_000_000, 12 * 1024**3)]),
        ])
        adapter._connected = True
        adapter._client = client

        profiles = await adapter.table_profiles()
        assert profiles["events"].row_count == 5_000_000
        assert profiles["events"].bytes == 12 * 1024**3
        assert profiles["events"].capabilities == frozenset({"row_count", "bytes"})

    async def test_one_query_for_the_whole_schema(self, monkeypatch):
        """批量取,不是每张表一个往返 —— 画像是增强,不该拖慢主链路(§10)。"""
        adapter, client = _ch_adapter(monkeypatch, [
            FakeResult(["name", "total_rows", "total_bytes"],
                       [("a", 1, 10), ("b", 2, 20)]),
        ])
        adapter._connected = True
        adapter._client = client

        await adapter.table_profiles()
        assert len(client.queries) == 1

    async def test_null_engine_stats_yield_none_not_zero(self, monkeypatch):
        """ClickHouse 对从未写入过的表返回 0/None —— 两者都当**没有依据**。"""
        adapter, client = _ch_adapter(monkeypatch, [
            FakeResult(["name", "total_rows", "total_bytes"],
                       [("empty", 0, 0), ("nulled", None, None)]),
        ])
        adapter._connected = True
        adapter._client = client

        profiles = await adapter.table_profiles()
        assert profiles["empty"].row_count is None
        assert profiles["nulled"].bytes is None
        # 字段没有值 ≠ 能力不存在(见 TestBaseContract 的第一条)
        assert "bytes" in profiles["empty"].capabilities
