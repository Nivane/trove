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

    逐条写死而不是「推」,是因为这张矩阵是**承诺书**:下游照它决定要不要依赖
    某个字段、运维照它决定往哪儿修。写死的代价是改的时候要动测试 —— 那正是
    我们想要的摩擦。
    """

    #: 适配器 → 它实现的字段采集。
    EXPECTED = {
        SQLiteAdapter: {"row_count"},
        DuckDBAdapter: {"row_count"},
        PostgresAdapter: {"row_count", "bytes", "last_analyzed"},
        MySQLAdapter: {"row_count", "bytes", "last_modified"},
        DorisAdapter: {"row_count"},
        ClickHouseAdapter: {
            "row_count", "bytes", "partition_column",
            "partition_count", "latest_partition",
        },
    }

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

    @pytest.mark.parametrize("adapter_cls,expected", EXPECTED.items())
    def test_the_declared_field_set_is_exactly_this(self, adapter_cls, expected):
        assert set(adapter_cls.profile_capabilities) == expected

    def test_doris_narrows_what_it_inherits(self):
        """Doris 走 MySQL 协议 → 继承 ``MySQLAdapter``。**能力矩阵不跟着继承。**

        ``DATA_LENGTH`` / ``UPDATE_TIME`` 在 Doris 上是 FE 虚拟表里的列,值的
        行为无从验证(§8.2 B 只声明验证过的)。默认全拿就是替它承诺两个没人验过
        的字段 —— 而 ``UPDATE_TIME`` 一旦给垃圾值,就会变成对外的 ``data_as_of``。
        """
        assert DorisAdapter.profile_capabilities < MySQLAdapter.profile_capabilities
        assert DorisAdapter._PROFILE_COLS == ()

    def test_postgres_declares_no_last_modified(self):
        """PG 的目录里**没有**「数据最后修改时间」这个字段,所以不声明。

        它有 ``last_analyze`` —— 那是「什么时候统计的」。拿近义字段顶替是最好
        蒙混的一种谎:值是真的,含义是错的。两个字段各自的含义就写在这里。
        """
        assert "last_modified" not in PostgresAdapter.profile_capabilities
        assert "last_analyzed" in PostgresAdapter.profile_capabilities

    @pytest.mark.parametrize("adapter_cls", [MySQLAdapter, DorisAdapter])
    def test_the_mysql_family_declares_exactly_what_its_query_selects(self, adapter_cls):
        """声明与实现同源 —— 矩阵里多一个字段,SQL 就得跟着多一列,反之亦然。"""
        declared = set(adapter_cls.profile_capabilities)
        selected = {"row_count"} | {field for _, field, _ in adapter_cls._PROFILE_COLS}
        assert declared == selected


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
        item = self._scripted.pop(0) if self._scripted else FakeResult([], [])
        if isinstance(item, Exception):  # 让测试能脚本化"这条查询自己挂了"
            raise item
        return item

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
            FakeResult(["name", "total_rows", "total_bytes", "partition_key"],
                       [("events", 5_000_000, 12 * 1024**3, "")]),
            FakeResult(["table", "partition_count", "latest_partition"], []),
        ])
        adapter._connected = True
        adapter._client = client

        profiles = await adapter.table_profiles()
        assert profiles["events"].row_count == 5_000_000
        assert profiles["events"].bytes == 12 * 1024**3
        assert profiles["events"].capabilities == frozenset({
            "row_count", "bytes", "partition_column",
            "partition_count", "latest_partition",
        })

    async def test_constant_round_trips_not_one_per_table(self, monkeypatch):
        """批量取:**常数次**往返(两次系统表查询),不是每张表一次。

        画像是增强,不该拖慢主链路(§10)。两次而不是一次是因为行数/字节在
        ``system.tables``、分区在 ``system.parts``,两个来源互不依赖 —— 合成
        一个 JOIN 会让分区块的失败连带丢掉行数(见下面那条降级测试)。
        """
        adapter, client = _ch_adapter(monkeypatch, [
            FakeResult(["name", "total_rows", "total_bytes", "partition_key"],
                       [("a", 1, 10, ""), ("b", 2, 20, "")]),
            FakeResult(["table", "partition_count", "latest_partition"], []),
        ])
        adapter._connected = True
        adapter._client = client

        await adapter.table_profiles()
        assert len(client.queries) == 2

    async def test_null_engine_stats_yield_none_not_zero(self, monkeypatch):
        """ClickHouse 对从未写入过的表返回 0/None —— 两者都当**没有依据**。

        ``total_rows`` / ``total_bytes`` 都是 ``Nullable``(「取不到就是 NULL」),
        用 0 冒充「已知」会把一条扫全表的查询判成零成本。
        """
        adapter, client = _ch_adapter(monkeypatch, [
            FakeResult(["name", "total_rows", "total_bytes", "partition_key"],
                       [("empty", 0, 0, ""), ("nulled", None, None, "")]),
            FakeResult(["table", "partition_count", "latest_partition"], []),
        ])
        adapter._connected = True
        adapter._client = client

        profiles = await adapter.table_profiles()
        assert profiles["empty"].row_count is None
        assert profiles["nulled"].bytes is None
        # 字段没有值 ≠ 能力不存在(见 TestBaseContract 的第一条)
        assert "bytes" in profiles["empty"].capabilities

    async def test_reports_partitions(self, monkeypatch):
        """分区是 ClickHouse 上**唯一**诚实的新鲜度信号(§8.4 A)。

        ``last_modified`` 在这个引擎上反映的是元数据变更(加了个分区、改了
        TTL),不代表数据到哪儿了;而 ``system.parts.partition`` 就是分区键的
        取值 —— ``toYYYYMM(dt)`` 之下就是 ``202609``。
        """
        adapter, client = _ch_adapter(monkeypatch, [
            FakeResult(["name", "total_rows", "total_bytes", "partition_key"],
                       [("events", 5_000_000, 12 * 1024**3, "toYYYYMM(dt)")]),
            FakeResult(["table", "partition_count", "latest_partition"],
                       [("events", 30, "202609")]),
        ])
        adapter._connected = True
        adapter._client = client

        p = (await adapter.table_profiles())["events"]
        assert p.partition_column == "toYYYYMM(dt)"
        assert p.partition_count == 30
        assert p.latest_partition == "202609"
        assert p.last_modified is None  # 不拿元数据变更时间冒充数据时间

    async def test_unpartitioned_tables_carry_no_partition_facts(self, monkeypatch):
        """没分区的表在 ``system.parts`` 里的分区名是 ``tuple()`` —— 那是**哨兵**,
        不是值。

        放过去就是 I5 的反面:``freshness`` 会把字面量 ``tuple()`` 当成截止时间
        报给用户,而那比 ``null`` 更糟 —— 它看起来是个答案。
        """
        adapter, client = _ch_adapter(monkeypatch, [
            FakeResult(["name", "total_rows", "total_bytes", "partition_key"],
                       [("plain", 100, 4096, "")]),
            FakeResult(["table", "partition_count", "latest_partition"],
                       [("plain", 1, "tuple()")]),
        ])
        adapter._connected = True
        adapter._client = client

        p = (await adapter.table_profiles())["plain"]
        assert p.row_count == 100
        assert p.bytes == 4096
        assert p.partition_column is None
        assert p.partition_count is None
        assert p.latest_partition is None

    async def test_multi_expression_partition_keys_have_no_latest(self, monkeypatch):
        """分区键是多个表达式时,**不给** ``latest_partition``。

        ``max(partition)`` 是字符串比较。单键时 ``202609`` 这种取值可比;多键时
        ``partition`` 是 ``('202609', 'apac')`` 这样的序列化元组,取最大值得到的
        是任意一个分区,而不是最新的那个 —— 报出去就是把一个随机分区说成数据的
        截止点。分区**数量**不受影响,照报。

        判据是纯语法的(分区键里有逗号),不做日期嗅探(§14.10 的同类理由):
        误判方向只能是"少报一个 latest_partition",即低估。
        """
        adapter, client = _ch_adapter(monkeypatch, [
            FakeResult(["name", "total_rows", "total_bytes", "partition_key"],
                       [("multi", 100, 4096, "(toYYYYMM(dt), region)")]),
            FakeResult(["table", "partition_count", "latest_partition"],
                       [("multi", 2, "('202609', 'apac')")]),
        ])
        adapter._connected = True
        adapter._client = client

        p = (await adapter.table_profiles())["multi"]
        assert p.partition_count == 2
        assert p.latest_partition is None
        assert p.partition_column == "(toYYYYMM(dt), region)"

    async def test_a_broken_parts_query_keeps_the_cheap_fields(self, monkeypatch):
        """分区查询失败(权限/版本)→ 退到"没有分区信息",**不丢掉行数与字节**。

        这里刻意**不**整体失败:``system.parts`` 是这条链上更贵的那个查询,
        失败了不该把已经拿到的行数/字节一起扔了 —— 那会让第 2 档整个失效、
        直接掉到保守预算。少一个字段不是放行:估算与预算照旧生效。
        """
        adapter, client = _ch_adapter(monkeypatch, [
            FakeResult(["name", "total_rows", "total_bytes", "partition_key"],
                       [("events", 5_000_000, 12 * 1024**3, "toYYYYMM(dt)")]),
            RuntimeError("system.parts: Access denied"),
        ])
        adapter._connected = True
        adapter._client = client

        p = (await adapter.table_profiles())["events"]
        assert p.row_count == 5_000_000
        assert p.bytes == 12 * 1024**3
        assert p.partition_count is None
        assert p.latest_partition is None
