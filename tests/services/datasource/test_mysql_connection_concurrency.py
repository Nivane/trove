"""MySQL 适配器的**并发**契约:一条连接、多个协程(锁 + 有界重试)。

根因(评测日志语料里收敛出来的):

  一个适配器 = 一条 aiomysql 连接,而 agent 循环会**并行派发工具**
  (``trove/llm/agent_loop.py`` 的 ``asyncio.gather``),``probe_query`` /
  ``check_result`` 会同时落到同一个适配器上。两个协程等同一个 socket 的
  下一份数据,协议就此失步 —— 症状不是业务错误,而是:

    * ``(2013, 'Lost connection to MySQL server during query')``
    * ``RuntimeError: readexactly() called while another coroutine is already
      waiting for incoming data``(asyncio 自己的并发读签名)

这个文件钉三件事,每件都对应一条修法:

  1. **互斥** —— 并发进入适配器的调用在连接上不重叠。假连接会在重叠时抛
     那条 asyncio 报文,所以「不重叠」是**可观测**的(而不是"看起来没报错");
  2. **失步恢复** —— 连接级异常 + 读语句 → 弃旧建新(关闭、不复用)、重试一次;
  3. **有界** —— 重试只有一次;写语句一次都不重试;非连接类错误不重建连接。

假连接仿的是 aiomysql.Connection 的用法(``cursor()`` / ``ping()`` / 同步
``close()``)。驱动不真连:``_get_driver`` 被 monkeypatch 成排队吐连接的假
驱动 —— 与 ``tests/services/test_mysql_adapter.py`` 同一套风格。
"""

from __future__ import annotations

import asyncio
import os

import pytest

from trove.core.errors import DatasourceError, SQLExecutionError
from trove.services.datasource.adapters.mysql import MySQLAdapter


# ── 假驱动 ──────────────────────────────────────────────


class _OperationalError(Exception):
    """pymysql OperationalError 的最小仿形:``(errno, message)`` 放在 args 里。

    适配器的判定按**错误码**收窄(2013/2006/2014/2055),所以仿形必须把
    错误码放在 ``args[0]`` —— 只留一条 message 的假异常会让「按码判定」
    这条路径永远测不到。
    """

    def __init__(self, code: int, message: str):
        super().__init__(code, message)
        self.code = code


class FakeCursor:
    def __init__(self, conn: "FakeConn"):
        self._conn = conn
        self.description = None
        self._rows: list = []

    async def execute(self, sql, params=None):
        conn = self._conn
        await asyncio.sleep(conn.delay)   # 让重叠成为可能(没有锁时必被抓到)
        conn.executed.append(sql)
        if conn.is_internal(sql):
            conn.internal.append(sql)
            return
        if "INFORMATION_SCHEMA" in str(sql).upper():
            return                        # 内省查询:形状是元数据,不是用例的行
        if conn.statement_errors:
            raise conn.statement_errors.pop(0)
        self.description = conn.description
        self._rows = list(conn.rows)

    async def fetchall(self):
        return list(self._rows)

    async def fetchone(self):
        return self._rows[0] if self._rows else None

    async def close(self):
        self._conn._exit()


class FakeConn:
    """可**侦测重叠**的假连接:同一条连接被两个协程同时使用就抛 asyncio 那条报文。

    重叠检测用 ``in_flight`` 计数:``cursor()`` 进入、``cursor.close()`` 退出
    (适配器的每条路径都保证 close 在 finally 里),``ping()`` 同理 —— 一次
    线上操作的生命周期就是一次进出。真实驱动里那条 RuntimeError 来自两个协程
    抢同一个 StreamReader;这里来自 ``_enter``,语义相同。
    """

    #: 适配器自己的家务语句(建连探测 + 库侧超时 SET):不占业务响应队列。
    #: 形状由 ``tests/services/test_adapter_statement_timeout_contract.py`` 钉。
    INTERNAL_MARKERS = ("MAX_EXECUTION_TIME", "MAX_STATEMENT_TIME", "SELECT VERSION()")

    def __init__(
        self,
        *,
        rows=None,
        description=None,
        delay: float = 0.0,
        statement_errors=None,
        ping_error=None,
    ):
        self.rows = rows or []
        self.description = description
        self.delay = delay
        self.statement_errors = list(statement_errors or [])  # 业务语句按序抛
        self.ping_error = ping_error
        self.ping_count = 0
        self.executed: list[str] = []
        self.internal: list[str] = []
        self.closed = False
        self.in_flight = 0
        self.peak_in_flight = 0
        self.overlaps = 0

    @classmethod
    def is_internal(cls, sql) -> bool:
        upper = str(sql or "").upper()
        return any(m in upper for m in cls.INTERNAL_MARKERS)

    def _enter(self):
        if self.in_flight:
            self.overlaps += 1
            raise RuntimeError(
                "readexactly() called while another coroutine is already "
                "waiting for incoming data"
            )
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)

    def _exit(self):
        self.in_flight = max(0, self.in_flight - 1)

    async def ping(self, reconnect=True):
        self._enter()
        try:
            self.ping_count += 1
            await asyncio.sleep(self.delay)
            if self.ping_error:
                raise self.ping_error
        finally:
            self._exit()

    async def cursor(self):
        self._enter()
        return FakeCursor(self)

    def close(self):
        # 同步:对齐 aiomysql.Connection.close(生产 disconnect 不 await)。
        self.closed = True


class FakeDriver:
    """``connect()`` 按序吐 ``outcomes``;元素是异常就抛(模拟建不起来)。

    队列耗尽后:``default`` 为 None 时吐一条新假连接(记录在 ``conns``),
    否则抛 ``default``。用例靠 ``connect_calls`` 断言"重试有界"。
    """

    def __init__(self, outcomes=(), *, default=None):
        self._outcomes = list(outcomes)
        self.default = default
        self.connect_calls = 0
        self.connect_kwargs = None
        self.conns: list[FakeConn] = []

    async def connect(self, **kwargs):
        self.connect_calls += 1
        self.connect_kwargs = kwargs
        if self._outcomes:
            item = self._outcomes.pop(0)
        else:
            item = self.default
        if isinstance(item, BaseException):
            raise item
        if item is None:
            item = FakeConn()
        self.conns.append(item)
        return item


def make_adapter(monkeypatch, outcomes=(), *, default=None):
    driver = FakeDriver(outcomes, default=default)
    monkeypatch.setattr(MySQLAdapter, "_get_driver", staticmethod(lambda: driver))
    adapter = MySQLAdapter(
        name="test",
        config={
            "host": "127.0.0.1", "port": 3306,
            "user": "root", "password": "p", "database": "testdb",
        },
    )
    adapter._server_version = "8.0.36"
    return adapter, driver


# ── 1. 互斥:同一条连接不允许被两个协程同时使用 ────────────


class TestTheLockSerializesTheSharedConnection:
    async def test_the_fake_really_detects_overlap(self):
        """先钉假连接有牙:重叠进入**一定**会被抓到。

        这条不是测实现,是测"上面那条并发用例要是没锁会不会绿" —— 假连接
        抓不到重叠的话,互斥用例就是一条永远绿的装饰。
        """
        conn = FakeConn()
        cur = await conn.cursor()
        with pytest.raises(RuntimeError, match="another coroutine"):
            await conn.cursor()
        await cur.close()

        assert conn.overlaps == 1
        # 前一个使用者的生命周期结束后,同一条连接可以再次进入
        await (await conn.cursor()).close()
        assert conn.in_flight == 0

    async def test_concurrent_calls_do_not_overlap_on_the_connection(self, monkeypatch):
        """两个 execute + 一个 get_schema 并发:不许重叠,全部成功。

        并行派发是 agent 循环的常态(probe_query / check_result 同轮 gather),
        所以这里用 ``asyncio.gather`` 而不是"先 A 后 B"。修复前这条会以
        ``RuntimeError: readexactly() called while another coroutine ...`` 红掉。
        """
        conn = FakeConn(delay=0.01, rows=[[1]], description=[("n",)])
        adapter, _ = make_adapter(monkeypatch, [conn])
        await adapter.connect()

        exec_a, exec_b, schema = await asyncio.gather(
            adapter.execute("SELECT 1"),
            adapter.execute("SELECT 2"),
            adapter.get_schema(),
        )

        assert conn.overlaps == 0, "两个协程重叠使用同一条连接 = 协议失步"
        assert conn.peak_in_flight == 1, "同一时刻只允许一个协程在这条连接上"
        assert exec_a.rows == [[1]] and exec_b.rows == [[1]]
        assert schema.tables == []

    async def test_the_statement_timeout_entry_is_serialized_too(self, monkeypatch):
        """公共入口 ``apply_statement_timeout`` 也是线上操作:并发时同样不许重叠。

        它最终会在共享连接上发一条 ``SET SESSION ...``,所以走与其他段同一把
        锁。这条同时钉住"两条路都不重入":公共入口自己取锁,锁内调用方
        (建连 / ping)走私有实现 —— 重入的话(asyncio.Lock 不可重入)这里、
        以及所有路过建连路径的用例,都会直接挂死。
        """
        conn = FakeConn(delay=0.01, rows=[[1]], description=[("n",)])
        adapter, _ = make_adapter(monkeypatch, [conn])
        await adapter.connect()

        armed, result = await asyncio.gather(
            adapter.apply_statement_timeout(),
            adapter.execute("SELECT 1"),
        )

        assert armed is True, "会话变量语句被服务端接受了"
        assert result.rows == [[1]]
        assert conn.overlaps == 0
        assert any("MAX_EXECUTION_TIME" in s.upper() for s in conn.internal)

    async def test_cancelling_a_lock_waiter_does_not_block_the_holder(self, monkeypatch):
        """锁等待必须**可取消**,而且取消的等待者不许把锁带走。

        取消的是"排队等锁"的那个协程(客户端中止 / 上游 wait_for 超时),
        不是持锁者 —— 持锁者的查询照常跑完,之后适配器照常可用。
        """
        conn = FakeConn(delay=0.05, rows=[[1]], description=[("n",)])
        adapter, _ = make_adapter(monkeypatch, [conn])
        await adapter.connect()

        holder = asyncio.create_task(adapter.execute("SELECT 1"))
        await asyncio.sleep(0.01)            # 让 holder 先拿到锁
        waiter = asyncio.create_task(adapter.execute("SELECT 2"))
        await asyncio.sleep(0.01)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

        assert (await holder).rows == [[1]]
        # 锁没被取消的等待者带走:后面的调用照常
        assert (await adapter.execute("SELECT 3")).rows == [[1]]
        assert conn.overlaps == 0


# ── 2. 失步恢复:连接级异常 + 读语句 → 弃旧建新、重试一次 ──


class TestADesyncedConnectionIsDiscarded:
    async def test_a_lost_connection_is_discarded_and_the_read_retried(self, monkeypatch):
        """2013 → 旧连接关掉、不复用;换一条新的重试,**读语句成功**。"""
        broken = FakeConn(statement_errors=[
            _OperationalError(2013, "Lost connection to MySQL server during query"),
        ])
        healthy = FakeConn(rows=[[7]], description=[("n",)])
        adapter, driver = make_adapter(monkeypatch, [broken, healthy])
        await adapter.connect()

        result = await adapter.execute("SELECT n FROM t")

        assert result.rows == [[7]]
        assert broken.closed is True, "失步的连接必须被关闭,不能留在原地等下一次复用"
        assert broken.executed.count("SELECT n FROM t") == 1, "旧连接上只跑过那一次(失败的)"
        assert driver.connect_calls == 2, "重试 = 换一条新连接"
        assert adapter._conn is healthy
        assert healthy.executed.count("SELECT n FROM t") == 1

    async def test_an_eof_class_error_is_retried_too(self, monkeypatch):
        """EOF 一族(``asyncio.IncompleteReadError``)与 readexactly 同族:
        连接读到一半断了 = 已经不可信,照样弃旧建新。"""
        broken = FakeConn(statement_errors=[asyncio.IncompleteReadError(b"", 4)])
        healthy = FakeConn(rows=[[1]], description=[("n",)])
        adapter, driver = make_adapter(monkeypatch, [broken, healthy])
        await adapter.connect()

        assert (await adapter.execute("SELECT 1")).rows == [[1]]
        assert broken.closed is True
        assert driver.connect_calls == 2

    async def test_a_ping_failure_rebuilds_before_running_the_statement(self, monkeypatch):
        """ping 失败说明这条连接已经不工作(``reconnect=True`` 也救不回来)。

        此时**语句还没跑**,重建一次对任何语句都安全 —— 而且这条恢复对写
        语句同样成立(它重试的是连接,不是语句)。
        """
        stale = FakeConn(ping_error=_OperationalError(2006, "MySQL server has gone away"))
        fresh = FakeConn(rows=[[1]], description=[("n",)])
        adapter, driver = make_adapter(monkeypatch, [stale, fresh])
        await adapter.connect()

        assert (await adapter.execute("SELECT 1")).rows == [[1]]
        assert stale.closed is True
        assert driver.connect_calls == 2
        assert fresh.executed.count("SELECT 1") == 1

    async def test_a_rebuild_that_fails_surfaces_as_reconnect_failed(self, monkeypatch):
        """重建也失败(服务端真不可达)→ 如实抛,而且**有界**(只试了一次)。"""
        stale = FakeConn(ping_error=_OperationalError(2006, "MySQL server has gone away"))
        adapter, driver = make_adapter(
            monkeypatch, [stale, OSError("connection refused")],
            default=OSError("connection refused"),
        )
        await adapter.connect()

        with pytest.raises(DatasourceError, match="reconnect failed"):
            await adapter.execute("SELECT 1")

        assert driver.connect_calls == 2, "初次 + 一次重建,不循环重连"

    async def test_the_adapter_heals_itself_on_the_next_call(self, monkeypatch):
        """重建失败不是终态:下一次调用会再试一次(适配器没有变成一次性)。"""
        stale = FakeConn(ping_error=_OperationalError(2006, "MySQL server has gone away"))
        fresh = FakeConn(rows=[[5]], description=[("n",)])
        adapter, driver = make_adapter(
            monkeypatch, [stale, OSError("connection refused"), fresh],
        )
        await adapter.connect()

        with pytest.raises(DatasourceError, match="reconnect failed"):
            await adapter.execute("SELECT 1")

        # 服务端回来了:下一次调用自己重建、自己恢复
        assert (await adapter.execute("SELECT n FROM t")).rows == [[5]]
        assert adapter._conn is fresh


# ── 3. 有界:只重试一次;写语句与"非连接类错误"都不碰这一层 ──


class TestTheRetryBoundary:
    async def test_the_retry_happens_at_most_once(self, monkeypatch):
        """两次都 2013 → 抛出去,而不是无限重建;错误类型保持 SQLExecutionError。"""
        first = FakeConn(statement_errors=[
            _OperationalError(2013, "Lost connection to MySQL server during query"),
        ])
        second = FakeConn(statement_errors=[
            _OperationalError(2013, "Lost connection to MySQL server during query"),
        ])
        adapter, driver = make_adapter(monkeypatch, [first, second])
        await adapter.connect()

        with pytest.raises(SQLExecutionError) as exc_info:
            await adapter.execute("SELECT 1")

        assert "2013" in exc_info.value.db_error
        assert driver.connect_calls == 2, "只重建一次:重试是有界的,不是重连循环"
        assert second.closed is True, "第二次也失步 → 同样丢弃,留给下一次调用重建"

    async def test_write_statements_are_never_retried(self, monkeypatch):
        """写语句**不做任何自动重试**:失败的那条可能已经在服务端落了一半。

        连接照样丢弃(它已不可信),但语句不会再发第二遍 —— "丢连接"与
        "重发语句"是两件事,这里只做前一件。
        """
        conn = FakeConn(statement_errors=[
            _OperationalError(2013, "Lost connection to MySQL server during query"),
        ])
        adapter, driver = make_adapter(monkeypatch, [conn])
        await adapter.connect()

        with pytest.raises(SQLExecutionError):
            await adapter.execute("INSERT INTO t VALUES (1)")

        assert driver.connect_calls == 1, "写语句不许自动重发"
        assert conn.executed.count("INSERT INTO t VALUES (1)") == 1
        assert conn.closed is True, "连接仍要丢弃(下一条语句会用到新连接)"

    async def test_non_connection_errors_leave_the_connection_alone(self, monkeypatch):
        """权限拒绝(1045)是 ``OperationalError``,但**不是**连接失步。

        判定按错误码收窄而不是按异常类型名:驱动把权限/锁等待/缺表都塞进
        OperationalError,按类型名放行会把一条好连接拆掉重连,纯浪费。
        """
        conn = FakeConn(statement_errors=[
            _OperationalError(1045, "Access denied for user 'u'@'%'"),
        ])
        adapter, driver = make_adapter(monkeypatch, [conn])
        await adapter.connect()

        with pytest.raises(SQLExecutionError) as exc_info:
            await adapter.execute("SELECT 1")

        assert "1045" in exc_info.value.db_error
        assert conn.closed is False, "非连接类错误不该重建连接"
        assert driver.connect_calls == 1

    async def test_a_plain_statement_error_does_not_rebuild(self, monkeypatch):
        """无错误码、措辞也不像连接问题的异常:原样上抛(不宽泛 catch)。"""
        conn = FakeConn(statement_errors=[RuntimeError("Unknown column 'foo' in 'field list'")])
        adapter, driver = make_adapter(monkeypatch, [conn])
        await adapter.connect()

        with pytest.raises(SQLExecutionError):
            await adapter.execute("SELECT foo FROM t")

        assert conn.closed is False
        assert driver.connect_calls == 1


# ── 真库(env-gated):双探针并发 ────────────────────────────


@pytest.mark.integration
class TestMySQLConcurrencyIntegration:
    """``MYSQL_TEST_URL`` 未设即跳过(与 test_mysql_adapter.py 同一道门)。

    真库上,修复前这条会以 2013 / readexactly 一族红掉;修复后并发调用在
    适配器内串行,全部成功。
    """

    async def test_concurrent_probes_share_one_real_connection(self):
        url = os.environ.get("MYSQL_TEST_URL")
        if not url:
            pytest.skip("MYSQL_TEST_URL not set")
        from trove.services.datasource.urls import parse_datasource_url

        params = parse_datasource_url(url).connection_params
        adapter = MySQLAdapter(name=str(params.get("database") or "it"), config=params)
        await adapter.connect()
        try:
            a, b, schema, c = await asyncio.gather(
                adapter.execute("SELECT 1"),
                adapter.execute("SELECT 2"),
                adapter.get_schema(),
                adapter.execute("SELECT 1 AS n"),
            )
            assert a.rows == [[1]] and b.rows == [[2]]
            assert c.columns == ["n"] and c.rows == [[1]]
            assert schema.tables, "真库至少要内省出一张表"
        finally:
            await adapter.disconnect()
