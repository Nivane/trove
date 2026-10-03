"""MySQL adapter tests — unit (fake driver) + integration (MYSQL_TEST_URL)."""

import os
import uuid

import pytest

from trove.core.errors import DatasourceError, SQLExecutionError
from trove.core.types import BASIS_GRANTS, BASIS_PROBE_FAILED
from trove.services.datasource.adapters.mysql import MySQLAdapter


# ── Fake driver ──────────────────────────────────────────


class FakeCursor:
    """假游标:响应在**首次 execute** 时才定型。

    适配器在 connect 与每次 ping 之后会发一条**家务语句**(库侧语句超时的
    会话变量 SET,见 ``MySQLAdapter.supports_statement_timeout``);``cursor()``
    那一刻看不出它要执行什么,所以在 ``cursor()`` 处出队会让每条用例都得为
    它多排一份空响应,排队位置还会随适配器改动整体平移。改为首次 execute 时
    按 SQL 分流:用例的 ``cursor_specs`` 只需描述**自己的业务语句**。
    """

    def __init__(self, conn):
        self._conn = conn
        self._responses = None       # None = 尚未定型(首次 execute 决定)
        self.description = None
        self.error = None
        self.executed = []

    def _bind(self, sql: str) -> None:
        if self._responses is not None:
            return
        if self._conn.is_housekeeping(sql):
            # 家务语句:空响应足够(它不 fetch);单独记账备查,不占业务队列
            self._responses = []
            self._conn.housekeeping.append(self)
            return
        responses, self.description, self.error = self._conn._next_spec()
        self._responses = list(responses)
        self._conn.cursors.append(self)

    async def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self._bind(sql)
        if self.error:
            raise self.error

    async def fetchall(self):
        return self._responses.pop(0) if self._responses else []

    async def fetchone(self):
        return self._responses.pop(0) if self._responses else None

    async def close(self):
        pass


class FakeConn:
    #: 家务语句的判据(变量名,不是前缀 —— 免得把用例自己的 SET 也吞掉)。
    #: 形状由 ``test_adapter_statement_timeout_contract.py`` 专职钉住;这里
    #: 保证「发得出、关得掉」并且**看得见**(``housekeeping`` 记账)。
    HOUSEKEEPING_MARKERS = ("MAX_EXECUTION_TIME", "MAX_STATEMENT_TIME")

    def __init__(self, cursor_specs=None):
        # cursor_specs: 业务游标按创建顺序出队,每条 (responses, description, error)
        self._cursor_specs = list(cursor_specs or [])
        self.cursors = []        # 业务游标(索引与用例里的第 N 条语句一一对应)
        self.housekeeping = []   # 家务游标(见 HOUSEKEEPING_MARKERS)
        self.ping_count = 0
        self.ping_error = None

    @classmethod
    def is_housekeeping(cls, sql) -> bool:
        upper = str(sql or "").upper()
        return upper.startswith("SET SESSION ") and any(
            m in upper for m in cls.HOUSEKEEPING_MARKERS
        )

    def _next_spec(self):
        return self._cursor_specs.pop(0) if self._cursor_specs else ({}, None, None)

    async def ping(self, reconnect=True):
        self.ping_count += 1
        if self.ping_error:
            raise self.ping_error

    async def cursor(self):
        return FakeCursor(self)

    def close(self):
        # 同步:对齐 aiomysql.Connection.close(生产 disconnect 不 await);
        # 写成 async 会让用例里的直接调用产出 never-awaited 协程警告。
        pass


class FakeDriver:
    def __init__(self, conn=None, *, fail_after=None):
        self.conn = conn or FakeConn()
        self.connect_kwargs = None
        self.connect_calls = 0
        #: 第 N 次之后的 connect 一律失败(模拟服务端真的不可达)。
        #: 适配器在连接级故障后会重建一次,所以「重连失败」的现场是**新建
        #: 连接这一步也失败** —— 没有这个旋钮,fake 会把每次重建都"连接成功"。
        self.fail_after = fail_after

    async def connect(self, **kwargs):
        self.connect_calls += 1
        if self.fail_after is not None and self.connect_calls > self.fail_after:
            raise OSError("connection refused")
        self.connect_kwargs = kwargs
        return self.conn


def make_adapter(monkeypatch, driver=None, config=None, version="8.0.36"):
    driver = driver or FakeDriver()
    monkeypatch.setattr(MySQLAdapter, "_get_driver", staticmethod(lambda: driver))
    adapter = MySQLAdapter(
        name="test",
        config=config or {
            "host": "127.0.0.1", "port": 3306,
            "user": "root", "password": "p", "database": "testdb",
        },
    )
    adapter._server_version = version
    return adapter, driver


# ── Unit tests ───────────────────────────────────────────


class TestMySQLAdapter:
    async def test_connect_passes_parsed_params(self, monkeypatch):
        adapter, driver = make_adapter(monkeypatch)
        await adapter.connect()

        assert adapter.is_connected
        kwargs = driver.connect_kwargs
        assert kwargs["host"] == "127.0.0.1"
        assert kwargs["port"] == 3306
        assert kwargs["user"] == "root"
        assert kwargs["password"] == "p"
        assert kwargs["db"] == "testdb"

    async def test_connect_probes_version(self, monkeypatch):
        conn = FakeConn(cursor_specs=[([["8.0.36"]], None, None)])
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(conn))
        adapter._server_version = ""
        await adapter.connect()

        assert adapter._server_version == "8.0.36"
        assert conn.cursors[0].executed[0][0] == "SELECT VERSION()"

    async def test_connect_and_ping_arm_the_db_side_timeout(self, monkeypatch):
        """库侧兜底闸(§10):connect 时落一条会话变量,每次 ping 之后**再落一次**
        (ping 可能悄悄重连并带走会话变量,且无法从返回值区分)。

        语句形状由 ``test_adapter_statement_timeout_contract.py`` 专职钉;这里钉
        **本文件的假连接确实看见了它** —— 家务语句若被静默吞掉,"以为有界其实
        没有"的失败模式就在测试里原样成立(见 ``FakeConn.HOUSEKEEPING_MARKERS``)。
        """
        conn = FakeConn()
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(conn))
        await adapter.connect()
        await adapter.execute("SELECT 1")

        sent = [sql for c in conn.housekeeping for sql, _ in c.executed]
        assert sent == ["SET SESSION max_execution_time = 60000"] * 2

    async def test_connect_failure_wraps_datasource_error(self, monkeypatch):
        class BadDriver:
            async def connect(self, **kwargs):
                raise OSError("connection refused")

        monkeypatch.setattr(MySQLAdapter, "_get_driver", staticmethod(lambda: BadDriver()))
        adapter = MySQLAdapter(name="test", config={})
        with pytest.raises(DatasourceError):
            await adapter.connect()
        assert not adapter.is_connected

    async def test_execute_returns_query_result(self, monkeypatch):
        conn = FakeConn(cursor_specs=[
            ([["8.0.36"]], None, None),  # version probe
            ([[[1, "a"], [2, "b"]]], [("id",), ("name",)], None),  # execute
        ])
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(conn))
        await adapter.connect()

        result = await adapter.execute("SELECT id, name FROM t")
        assert result.columns == ["id", "name"]
        assert result.rows == [[1, "a"], [2, "b"]]
        assert result.row_count == 2
        assert result.datasource == "test"

    async def test_execute_error_wraps(self, monkeypatch):
        conn = FakeConn(cursor_specs=[
            ([["8.0.36"]], None, None),
            ([], None, RuntimeError("no such table")),
        ])
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(conn))
        await adapter.connect()

        with pytest.raises(SQLExecutionError) as exc_info:
            await adapter.execute("SELECT * FROM ghost")
        assert "no such table" in str(exc_info.value)

    async def test_execute_before_connect_raises(self, monkeypatch):
        adapter, _ = make_adapter(monkeypatch)
        with pytest.raises(SQLExecutionError):
            await adapter.execute("SELECT 1")

    async def test_execute_reconnects_stale_connection(self, monkeypatch):
        conn = FakeConn(cursor_specs=[
            ([["8.0.36"]], None, None),  # version probe
            ([[[1, "a"]]], [("id",)], None),  # execute after reconnect
        ])
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(conn))
        await adapter.connect()
        adapter._conn.close()  # simulate server dropping the idle connection

        result = await adapter.execute("SELECT id FROM t")
        assert result.rows == [[1, "a"]]
        assert conn.ping_count >= 1

    async def test_reconnect_failure_raises_datasource_error(self, monkeypatch):
        """服务端真的不可达 → 如实抛 ``reconnect failed``,**有界**。

        适配器在 ping 失败后会弃旧建新一次(语句还没跑,见 ``_ensure_connected``;
        自愈的那条路走 ``test_mysql_connection_concurrency.py``),所以"重连失败"
        的现场是**新的连接也建不起来** —— 假驱动必须在这一步失败:
        ``fail_after=1`` = 首次建连成功,之后一律拒绝。
        """
        conn = FakeConn()
        conn.ping_error = RuntimeError("connection refused")
        driver = FakeDriver(conn, fail_after=1)
        adapter, _ = make_adapter(monkeypatch, driver=driver)
        await adapter.connect()

        with pytest.raises(DatasourceError, match="reconnect failed"):
            await adapter.execute("SELECT 1")

        assert driver.connect_calls == 2, "初次 + 一次重建,不循环重连"
        assert adapter._conn is None  # 坏连接已丢弃,不留着等下一次复用

    async def test_get_schema_reconnects_stale_connection(self, monkeypatch):
        conn = FakeConn(cursor_specs=[
            ([["8.0.36"]], None, None),                       # version probe cursor
            (                                               # get_schema cursor (reused):
                [
                    [("students", 100)],
                    [("id", "int", "NO", "PRI")],
                ],
                None, None,
            ),
        ])
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(conn))
        await adapter.connect()
        adapter._conn.close()

        schema = await adapter.get_schema()
        assert len(schema.tables) == 1
        assert schema.tables[0].name == "students"
        assert conn.ping_count >= 1

    async def test_get_schema_introspects_information_schema(self, monkeypatch):
        conn = FakeConn(cursor_specs=[
            ([["8.0.36"]], None, None),                       # version probe cursor
            (                                               # get_schema cursor (reused):
                [
                    [("students", 100)],                     #   TABLES fetchall
                    [                                       #   COLUMNS fetchall
                        ("id", "int", "NO", "PRI"),
                        ("name", "varchar", "YES", ""),
                    ],
                ],
                None, None,
            ),
        ])
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(conn))
        await adapter.connect()

        schema = await adapter.get_schema()
        assert len(schema.tables) == 1
        table = schema.tables[0]
        assert table.name == "students"
        assert table.row_count_estimate == 100

        # introspection went through information_schema
        executed_sql = " ".join(
            sql for cur in conn.cursors for sql, _ in cur.executed
        )
        assert "information_schema.TABLES" in executed_sql
        assert "information_schema.COLUMNS" in executed_sql

        id_col = next(c for c in table.columns if c.name == "id")
        assert id_col.primary_key is True
        assert id_col.nullable is False

        name_col = next(c for c in table.columns if c.name == "name")
        assert name_col.nullable is True
        assert name_col.primary_key is False

    async def test_get_capabilities_mysql8(self, monkeypatch):
        adapter, _ = make_adapter(monkeypatch, version="8.0.36")
        caps = await adapter.get_capabilities()
        assert caps.dialect == "mysql"
        assert caps.supports_cte is True
        assert caps.supports_window_functions is True
        assert caps.supports_json_type is True

    async def test_get_capabilities_mysql57(self, monkeypatch):
        adapter, _ = make_adapter(monkeypatch, version="5.7.44")
        caps = await adapter.get_capabilities()
        assert caps.supports_cte is False
        assert caps.supports_window_functions is False

    async def test_dialect_static(self):
        assert MySQLAdapter.dialect() == "mysql"

    async def test_missing_driver_hint(self, monkeypatch):
        def _missing():
            raise DatasourceError(
                message="aiomysql is not installed — run `uv sync --extra mysql`",
                datasource="",
            )

        monkeypatch.setattr(MySQLAdapter, "_get_driver", staticmethod(_missing))
        adapter = MySQLAdapter(name="test", config={})
        with pytest.raises(DatasourceError) as exc_info:
            await adapter.connect()
        assert "uv sync --extra mysql" in str(exc_info.value)


class TestMySQLReadonlyProbe:
    """只读自检的 MySQL 实现(设计 §4 I1)。

    ``SHOW GRANTS`` 的一行长这样::

        GRANT SELECT, SHOW VIEW ON `db`.* TO `u`@`%` WITH GRANT OPTION

    权限清单在 ``GRANT`` 与 `` ON `` 之间,**后面还有表名和账号名**。整行扫关键字
    会把 ``... ON `db`.`insert_log` ...`` 里的表名认成一个写权限 —— 于是一个纯
    只读账号被判成可写。下面第二条用例就是钉这个。
    """

    @staticmethod
    def _probe_conn(grants):
        """一个已经连上、且下一次 cursor 会吐出这些授权的连接。

        ``[[g] for g in grants]`` 是**行**的列表(SHOW GRANTS 一行一列),外面
        那层才是 ``FakeCursor`` 记的一次 ``fetchall`` 返回值 —— 少包一层的话
        ``fetchall`` 会直接吐出一行,而 ``probe_readonly`` 遍历到的就成了**这行
        字符串的字符**。写这个 helper 时就是这么错的:``'GRANT ...'`` 变成 ``'G'``,
        只读那几条用例因为 ``'G'`` 里当然没有写关键字而**全绿** —— 假驱动把形状
        说错了,测试就少测一层。
        """
        rows = [[g] for g in grants]
        return FakeConn(cursor_specs=[
            ([["8.0.36"]], None, None),  # connect 的版本探测
            ([rows], None, None),        # SHOW GRANTS 的一次 fetchall
        ])

    async def test_a_read_only_account_is_verified(self, monkeypatch):
        conn = self._probe_conn([
            "GRANT SELECT, SHOW VIEW ON `db`.* TO `u`@`%`",
            "GRANT USAGE ON *.* TO `u`@`%`",
        ])
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(conn))
        await adapter.connect()

        probe = await adapter.probe_readonly()

        assert probe.verified is True, "只读账号必须被判成只读"
        assert probe.basis == BASIS_GRANTS

    async def test_a_table_name_is_not_a_privilege(self, monkeypatch):
        """**这条是这一层的核心陷阱**:``GRANT SELECT ON `db`.`insert_log```
        里的 ``insert_log`` 是表名,不是 INSERT 权限。

        整行匹配关键字的实现会把一个纯只读账号报成「写得动」—— 方向上是保守的
        (误报故障),但同样是错,而且会让真出现的那条 WARN 失去意义。
        """
        conn = self._probe_conn([
            "GRANT SELECT ON `db`.`insert_log` TO `u`@`%`",
            "GRANT SELECT ON `db`.`create_table_audit` TO `u`@`%`",
        ])
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(conn))
        await adapter.connect()

        assert (await adapter.probe_readonly()).verified is True

    async def test_an_account_that_can_write_is_reported_as_writable(self, monkeypatch):
        conn = self._probe_conn([
            "GRANT SELECT, INSERT, UPDATE, DELETE ON `db`.* TO `u`@`%`",
        ])
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(conn))
        await adapter.connect()

        probe = await adapter.probe_readonly()

        assert probe.verified is False, "写得动的账号不能被说成只读"
        assert "INSERT" in probe.detail  # 依据要留在 detail 里,便于排查

    async def test_all_privileges_counts_as_writable(self, monkeypatch):
        conn = self._probe_conn(["GRANT ALL PRIVILEGES ON *.* TO `u`@`%`"])
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(conn))
        await adapter.connect()

        assert (await adapter.probe_readonly()).verified is False

    async def test_grant_option_is_not_a_read_only_boundary(self, monkeypatch):
        """``WITH GRANT OPTION`` 的账号**能自己把 INSERT 授给自己**。

        所以它不是一道只读边界 —— 今天没写权限不代表明天没有,而 I1 断言的正是
        「这道边界存在」。判成 ``False`` 会让它在启动日志里 WARN 一次,那是这里
        唯一安全的失败方向。
        """
        conn = self._probe_conn([
            "GRANT SELECT ON `db`.* TO `u`@`%` WITH GRANT OPTION",
        ])
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(conn))
        await adapter.connect()

        assert (await adapter.probe_readonly()).verified is False

    async def test_the_probe_never_sends_a_write_statement(self, monkeypatch):
        """**探测绝不试写**(§4 I1 的实施取舍)。

        设计稿给的另一条路是「尝试一条必然失败的写语句」—— 它「必然失败」的前提
        正是「账号只读」这个待证假设。账号其实可写时,那条语句会**真的写进去**:
        一个探测变成了生产写入。这条用例把「只查权限表」钉死在实现上。
        """
        conn = self._probe_conn(["GRANT SELECT ON `db`.* TO `u`@`%`"])
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(conn))
        await adapter.connect()
        await adapter.probe_readonly()

        sent = [sql for sql, _ in conn.cursors[1].executed]
        assert sent == ["SHOW GRANTS"], sent
        upper = sent[0].upper()
        for verb in ("INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER"):
            assert verb not in upper

    async def test_no_rows_at_all_is_unknown_not_read_only(self, monkeypatch):
        """一条授权都没看到 → **不知道**,不是「确认只读」。

        空结果最可能的解释是这条路径没拿到东西(驱动/权限/方言),而不是「这个
        账号什么都不能干」。往 ``True`` 倒就是替一道边界背书。
        """
        conn = FakeConn(cursor_specs=[
            ([["8.0.36"]], None, None),
            ([], None, None),
        ])
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(conn))
        await adapter.connect()

        probe = await adapter.probe_readonly()

        assert probe.verified is None
        assert probe.basis == BASIS_PROBE_FAILED

    async def test_an_operator_error_is_folded_by_the_orchestrator(self, monkeypatch):
        """查询被拒(权限不足)→ 适配器如实抛,由 ``readonly.probe`` 折成不知道。"""
        conn = FakeConn(cursor_specs=[
            ([["8.0.36"]], None, None),
            ([], None, RuntimeError("SHOW GRANTS command denied")),
        ])
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(conn))
        await adapter.connect()

        with pytest.raises(RuntimeError):
            await adapter.probe_readonly()

        from trove.services.datasource import readonly
        probe = await readonly.probe(adapter)
        assert probe.verified is None
        assert probe.basis == BASIS_PROBE_FAILED


# ── Integration tests (MYSQL_TEST_URL, skipped when unset) ──


@pytest.mark.integration
class TestMySQLIntegration:
    @pytest.fixture
    async def mysql_env(self):
        url = os.environ.get("MYSQL_TEST_URL")
        if not url:
            pytest.skip("MYSQL_TEST_URL not set")
        from trove.services.datasource.urls import parse_datasource_url
        cfg = parse_datasource_url(url)
        return cfg

    async def test_full_lifecycle_against_real_mysql(self, mysql_env):
        import aiomysql

        params = mysql_env.connection_params
        test_db = f"trove_test_{uuid.uuid4().hex[:8]}"

        # Isolated test database (requires CREATE DATABASE rights)
        admin = await aiomysql.connect(
            host=params["host"], port=params["port"],
            user=params["user"], password=params["password"],
        )
        try:
            cur = await admin.cursor()
            await cur.execute(f"CREATE DATABASE `{test_db}`")
            await cur.close()

            adapter = MySQLAdapter(name=test_db, config={**params, "database": test_db})
            await adapter.connect()
            try:
                await adapter.execute(
                    "CREATE TABLE students (id INT PRIMARY KEY, name VARCHAR(64) NOT NULL)"
                )
                await adapter.execute(
                    "INSERT INTO students VALUES (1, 'Alice'), (2, 'Bob')"
                )

                result = await adapter.execute("SELECT * FROM students ORDER BY id")
                assert result.row_count == 2
                assert result.columns == ["id", "name"]

                schema = await adapter.get_schema()
                table = next(t for t in schema.tables if t.name == "students")
                id_col = next(c for c in table.columns if c.name == "id")
                assert id_col.primary_key is True
            finally:
                await adapter.disconnect()
        finally:
            cur = await admin.cursor()
            await cur.execute(f"DROP DATABASE IF EXISTS `{test_db}`")
            await cur.close()
            admin.close()


class TestMySQLProfile:
    """MySQL 的画像字段(执行画像 §8.2 B / §14.12)。

    两个字段都有**官方文档写明的失效方式**,所以这里的测试是照着文档写的:

    * ``DATA_LENGTH``:For InnoDB, it is "the approximate amount of space
      allocated for the clustered index" —— 对 InnoDB 就是表本身,量纲与
      PG 的 ``pg_relation_size`` 对齐(都只算数据,不算二级索引)。
    * ``UPDATE_TIME``:"displays a timestamp value for the last UPDATE, INSERT,
      or DELETE performed on InnoDB tables **that are not partitioned**",
      而且 "Timestamps are not persisted when the server is restarted or when
      the table is evicted from the InnoDB data dictionary cache."
      → 缺失是常态,不是异常;缺失时必须说「不可得」。
    """

    async def test_missing_stats_are_none_not_zero(self, monkeypatch):
        """``TABLE_ROWS`` 为 NULL(统计没收集)时不能报 0。

        存量在**源头**就同形了 —— SQL 写着 ``IFNULL(TABLE_ROWS, 0)``:
        「统计缺失」和「空表」在到达适配器之前就已经长得一模一样,下游再怎么
        归一也分不出。修法是把 NULL 原样带出来。
        """
        conn = FakeConn(cursor_specs=[
            ([["8.0.36"]], None, None),          # connect: SELECT VERSION()
            ([[("t", None)], []], None, None),   # 画像表清单 + 列(空)
        ])
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(conn))
        await adapter.connect()

        schema = await adapter.get_schema()
        assert schema.tables[0].row_count_estimate is None

        sql = " ".join(s for c in conn.cursors for s, _ in c.executed)
        assert "IFNULL" not in sql

    async def test_profile_reports_bytes_and_last_modified(self, monkeypatch):
        from datetime import datetime

        conn = FakeConn(cursor_specs=[
            ([["8.0.36"]], None, None),          # connect: SELECT VERSION()
            ([
                [("events", 5_000_000, 12 * 1024**3, datetime(2026, 9, 28, 3, 15, 9))],
            ], None, None),                      # table_profiles
        ])
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(conn))
        await adapter.connect()

        p = (await adapter.table_profiles())["events"]
        assert p.row_count == 5_000_000
        assert p.bytes == 12 * 1024**3
        assert p.last_modified == "2026-09-28T03:15:09"

        sql = " ".join(s for c in conn.cursors for s, _ in c.executed)
        assert "DATA_LENGTH" in sql
        assert "UPDATE_TIME" in sql

    async def test_absent_timestamps_are_not_values(self, monkeypatch):
        """分区表 / 从未更新 / 零日期 —— 三种「没值」都要落到 ``None``。

        零日期那一行是本测试的重点:``0000-00-00 00:00:00`` 读起来像一个值,
        放过去会流进 ``freshness`` 的 ``min()``,把整片数据的截止时间拉到公元
        0 年 —— 格式还是对的,用户看不出来。
        """
        conn = FakeConn(cursor_specs=[
            ([["8.0.36"]], None, None),          # connect: SELECT VERSION()
            ([
                [("partitioned", 100, 4096, None),
                 ("never_updated", 100, 4096, "0000-00-00 00:00:00")],
            ], None, None),                      # table_profiles
        ])
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(conn))
        await adapter.connect()

        profiles = await adapter.table_profiles()
        for name in ("partitioned", "never_updated"):
            assert profiles[name].last_modified is None, name
            assert profiles[name].bytes == 4096, name
        # 时间戳缺了,但**能力还在** —— 「这张表没有值」不是「这个库给不出」
        assert "last_modified" in profiles["partitioned"].capabilities
