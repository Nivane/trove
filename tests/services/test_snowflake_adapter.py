"""Snowflake 适配器单测 —— **假驱动只照真驱动的契约说话**,不照我们希望的样子。

这个方言与其余几个的结构差在根上:驱动是**同步**的(snowflake-connector-
python 没有 asyncio 变体),五个方法全靠 ``asyncio.to_thread`` 包住。所以这里
第一件要钉的就是那条包装真的成立 —— 工作线程卡住时事件循环必须还能推进;
否则"取消只是放弃等待"这句话之下,整个进程会陪着那条查询一起卡住。

第二件钉的是终止(§7.3 / I4):abort 按**我们注入的 requestId** 打。为什么
不用 ``cursor.sfqid`` —— 同步 ``execute`` 的 sfqid 要等 ``cmd_query`` 返回才被
驱动赋值(4.8.0 源码 cursor.py:1039 实测),查询在飞时它是 ``None``:拿不到的
就是拿不到。所以假连接**不提供 sfqid**("想拿也拿不到"与真驱动同一形状),
``_cancel_query(sql, request_id)`` 才是它认得的那条路(驱动自己的超时定时器与
SIGINT 处理器就走的这一条)。

**服务端语义本地验不了** —— abort 是否真让查询停下、STATEMENT_TIMEOUT 是否真
生效,都只有 env-gated 的 ``SNOWFLAKE_TEST_URL`` 集成用例(CI 不跑),文档里
明写「未在真实云仓验证」。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import uuid

import pytest

from trove.core.errors import DatasourceError, SQLExecutionError
from trove.core.types import BASIS_UNVERIFIABLE
from trove.services.datasource.adapters import snowflake as sf_module
from trove.services.datasource.adapters.snowflake import (
    SnowflakeAdapter,
    _is_read_statement,
)

DEFAULT_CONFIG = {
    "account": "xy12345",
    "user": "trove_user",
    "password": "hunter2",
    "database": "FIN",
    "schema": "PUBLIC",
}


class FakeCursor:
    """真驱动游标的**我们用到的**那一面:execute / fetchall / description / close。

    ``execute`` 的 ``_statement_params`` 是**位置之后的关键字**——真驱动签名里
    它叫这个名字(cursor.py:667 实测),requestId 就从这里进。description 照
    真形状给 7 元组的前两列足够(适配器只取 ``d[0]``)。
    """

    def __init__(self, conn: "FakeConn"):
        self.conn = conn
        self.closed = False
        self.description = None
        self._rows: list = []

    def execute(self, sql, _statement_params=None):
        self.conn.statements.append((sql, _statement_params))
        self.conn.started.set()
        if self.conn.blocking is not None:
            self.conn.blocking.wait(timeout=10)
        if self.conn.error is not None:
            if self.conn.die_on_error:
                self.conn.closed = True   # 连接"当场死掉"
            raise self.conn.error
        if self.conn.scripted:
            self.description, self._rows = self.conn.scripted.pop(0)
        else:
            self.description = self.conn.default_description
            self._rows = list(self.conn.default_rows)

    def fetchall(self):
        return list(self._rows)

    def close(self):
        self.closed = True


class FakeConn:
    """连接受控的假雪花连接。刻意**没有** ``sfqid``(见模块 docstring)。"""

    def __init__(self, *, scripted=None, default_description=None,
                 default_rows=None, error=None, die_on_error=False,
                 blocking=None, abort_result=None, abort_error=None):
        # scripted: [(description, rows)] 每次 execute 弹一条;弹完用 default
        self.scripted = list(scripted or [])
        self.default_description = default_description
        self.default_rows = list(default_rows or [])
        self.error = error
        self.die_on_error = die_on_error
        self.blocking = blocking
        self.abort_result = {"success": True} if abort_result is None else abort_result
        self.abort_error = abort_error

        self.statements: list[tuple] = []   # (sql, _statement_params)
        self.started = threading.Event()
        self.aborts: list[tuple] = []       # (sql, request_id)
        self.close_args: list = []          # close() 的 retry 实参
        self.closed = False
        self.expired = False

    # ── 驱动连接面 ──────────────────────────────
    def cursor(self):
        return FakeCursor(self)

    def is_closed(self):
        return self.closed

    def close(self, retry=True):
        self.close_args.append(retry)
        self.closed = True

    def _cancel_query(self, sql, request_id):
        self.aborts.append((sql, request_id))
        if self.abort_error is not None:
            raise self.abort_error
        return self.abort_result


class FakeDriver:
    """模块级 ``connect(**)`` 是它唯一的入口(与 ``snowflake.connector`` 同)。"""

    def __init__(self, conns=None):
        self._conns = list(conns or [])
        self.connect_calls: list[dict] = []   # 每次建连的 kwargs

    def connect(self, **kwargs):
        self.connect_calls.append(kwargs)
        return self._conns.pop(0) if self._conns else FakeConn()


def make_adapter(monkeypatch, driver=None, config=None):
    driver = driver or FakeDriver()
    monkeypatch.setattr(sf_module, "_get_driver", lambda: driver)
    adapter = SnowflakeAdapter(name="test", config=config or dict(DEFAULT_CONFIG))
    return adapter, driver


# ── 建连 ─────────────────────────────────────────────────


class TestConnect:
    async def test_login_params_travel_together(self, monkeypatch):
        adapter, driver = make_adapter(monkeypatch, config={
            **DEFAULT_CONFIG, "warehouse": "COMPUTE_WH", "role": "ANALYST",
            "private_key_file": "/keys/trove.p8", "port": 443,
        })
        await adapter.connect()

        assert adapter.is_connected
        kwargs = driver.connect_calls[0]
        assert kwargs["account"] == "xy12345"
        assert kwargs["user"] == "trove_user"
        assert kwargs["password"] == "hunter2"
        assert kwargs["database"] == "FIN"
        assert kwargs["schema"] == "PUBLIC"
        assert kwargs["warehouse"] == "COMPUTE_WH"
        assert kwargs["role"] == "ANALYST"
        assert kwargs["private_key_file"] == "/keys/trove.p8"
        assert kwargs["port"] == 443

    async def test_unset_optionals_stay_out_of_the_login_body(self, monkeypatch):
        """没配 warehouse/role 时不带键 —— 缺省交给驱动,不拿空串覆盖它。"""
        adapter, driver = make_adapter(monkeypatch)
        await adapter.connect()

        kwargs = driver.connect_calls[0]
        assert "warehouse" not in kwargs
        assert "role" not in kwargs
        assert "private_key_file" not in kwargs
        # 固定 pyformat:我们不传绑定参数,选它是为了 % 行为确定（见适配器注释）
        assert kwargs["paramstyle"] == "pyformat"

    async def test_a_missing_account_is_a_config_error(self, monkeypatch):
        adapter, driver = make_adapter(
            monkeypatch, config={k: v for k, v in DEFAULT_CONFIG.items() if k != "account"},
        )
        with pytest.raises(DatasourceError, match="account"):
            await adapter.connect()
        assert driver.connect_calls == []

    async def test_a_failing_login_wraps_datasource_error(self, monkeypatch):
        class BadDriver:
            def connect(self, **kwargs):
                raise OSError("account not found")

        monkeypatch.setattr(sf_module, "_get_driver", lambda: BadDriver())
        adapter = SnowflakeAdapter(name="test", config=dict(DEFAULT_CONFIG))
        with pytest.raises(DatasourceError, match="account not found"):
            await adapter.connect()
        assert not adapter.is_connected

    async def test_the_missing_driver_carries_the_install_hint(self, monkeypatch):
        def _missing():
            raise DatasourceError(
                message="snowflake-connector-python is not installed — run "
                        "`uv sync --extra snowflake`",
                datasource="",
            )

        monkeypatch.setattr(sf_module, "_get_driver", _missing)
        adapter = SnowflakeAdapter(name="test", config=dict(DEFAULT_CONFIG))
        with pytest.raises(DatasourceError) as exc_info:
            await adapter.connect()
        assert "uv sync --extra snowflake" in str(exc_info.value)

    def test_the_real_get_driver_reports_the_hint_when_import_fails(self, monkeypatch):
        """真 ``_get_driver`` 自己的提示(不是测试替身演的)。

        ``sys.modules`` 里放 None 是让 ``import snowflake.connector`` 必然
        ImportError 的标准做法 —— 不管这台机器装没装驱动,这条都成立。
        """
        monkeypatch.setitem(sys.modules, "snowflake", None)
        monkeypatch.setitem(sys.modules, "snowflake.connector", None)
        with pytest.raises(DatasourceError) as exc_info:
            sf_module._get_driver()
        assert "uv sync --extra snowflake" in str(exc_info.value)

    async def test_disconnect_closes_without_letting_close_retry(self, monkeypatch):
        conn = FakeConn()
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver([conn]))
        await adapter.connect()
        await adapter.disconnect()

        assert not adapter.is_connected
        assert conn.close_args == [False]   # close(retry=False):关的就是已知坏连接


# ── 执行(含 to_thread 包装)─────────────────────────────


class TestExecute:
    async def test_execute_returns_query_result(self, monkeypatch):
        conn = FakeConn(
            default_description=[("ID", 0), ("NAME", 0)],
            default_rows=[[1, "a"], [2, "b"]],
        )
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver([conn]))
        await adapter.connect()

        result = await adapter.execute("SELECT id, name FROM t")
        assert result.columns == ["ID", "NAME"]
        assert result.rows == [[1, "a"], [2, "b"]]
        assert result.row_count == 2
        assert result.datasource == "test"

    async def test_execute_injects_a_uuid4_request_id(self, monkeypatch):
        """abort 的靶子就是这个 id —— 形状必须是 uuid4(驱动会校验,见适配器
        docstring 的版本 floor 说明)。"""
        conn = FakeConn(default_description=[("X", 0)], default_rows=[[1]])
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver([conn]))
        await adapter.connect()

        await adapter.execute("SELECT 1")

        sql, params = conn.statements[0]
        assert sql == "SELECT 1"
        assert set(params) == {"requestId"}
        injected = uuid.UUID(params["requestId"])
        assert injected.version == 4

    async def test_the_event_loop_advances_while_the_driver_works(self, monkeypatch):
        """同步驱动被包在线程里 —— 工作线程卡住时事件循环还能跑别的协程。

        去掉 ``to_thread`` 的回归表现:别的协程全部停摆(这条用例在超时前
        永远数不到 tick)。这正是"取消只是放弃等待"之下必须成立的前提 ——
        等不到这一步,连取消的机会都不会有。
        """
        release = threading.Event()
        conn = FakeConn(blocking=release, default_rows=[[1]],
                        default_description=[("X", 0)])
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver([conn]))
        await adapter.connect()

        async def _while_blocked() -> int:
            task = asyncio.create_task(adapter.execute("SELECT 1"))
            while not conn.started.is_set():
                await asyncio.sleep(0.01)
            ticks = 0
            for _ in range(5):          # 工作线程卡着,循环必须照转
                await asyncio.sleep(0.01)
                ticks += 1
            release.set()
            assert (await task).rows == [[1]]
            return ticks

        assert await asyncio.wait_for(_while_blocked(), timeout=5) == 5

    async def test_execute_before_connect_raises(self, monkeypatch):
        adapter, _ = make_adapter(monkeypatch)
        with pytest.raises(SQLExecutionError):
            await adapter.execute("SELECT 1")

    async def test_execute_error_wraps_into_sql_execution_error(self, monkeypatch):
        conn = FakeConn(error=RuntimeError("Object 'GHOST' does not exist"))
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver([conn]))
        await adapter.connect()

        with pytest.raises(SQLExecutionError) as exc_info:
            await adapter.execute("SELECT * FROM ghost")
        assert "GHOST" in str(exc_info.value)


class TestConnectionHealth:
    """连接可用性的三个事实源(closed / expired / None)——纯属性读,零往返。"""

    def test_connection_unusable_matrix(self, monkeypatch):
        adapter, _ = make_adapter(monkeypatch)
        assert adapter._connection_unusable() is True      # 没连接

        healthy = FakeConn()
        adapter._conn = healthy
        assert adapter._connection_unusable() is False

        healthy.closed = True
        assert adapter._connection_unusable() is True      # is_closed()

        expired = FakeConn()
        expired.expired = True
        adapter._conn = expired
        assert adapter._connection_unusable() is True      # 主令牌过期

    async def test_an_expired_connection_is_rebuilt_before_the_statement(self, monkeypatch):
        expired = FakeConn(default_rows=[[1]], default_description=[("X", 0)])
        expired.expired = True
        fresh = FakeConn(default_rows=[[2]], default_description=[("X", 0)])
        adapter, driver = make_adapter(
            monkeypatch, driver=FakeDriver([expired, fresh]),
        )
        await adapter.connect()

        result = await adapter.execute("SELECT 1")

        assert result.rows == [[2]]                 # 在新连接上跑的
        assert len(driver.connect_calls) == 2
        assert expired.close_args == [False]        # 坏连接丢弃,不复用

    async def test_a_read_statement_is_retried_once_on_a_fresh_connection(self, monkeypatch):
        dead = FakeConn(error=RuntimeError("connection reset"), die_on_error=True)
        fresh = FakeConn(default_rows=[[1]], default_description=[("X", 0)])
        adapter, driver = make_adapter(monkeypatch, driver=FakeDriver([dead, fresh]))
        await adapter.connect()

        result = await adapter.execute("SELECT 1")

        assert result.rows == [[1]]
        assert len(driver.connect_calls) == 2
        assert dead.close_args == [False]

    async def test_a_write_statement_is_dropped_but_never_retried(self, monkeypatch):
        """丢弃 ≠ 重发:写语句"执行了没有"不可判,重发可能产生第二次副作用。"""
        dead = FakeConn(error=RuntimeError("connection reset"), die_on_error=True)
        adapter, driver = make_adapter(monkeypatch, driver=FakeDriver([dead]))
        await adapter.connect()

        with pytest.raises(SQLExecutionError):
            await adapter.execute("INSERT INTO t VALUES (1)")

        assert len(driver.connect_calls) == 1       # 没有重连(没有重发)
        assert dead.close_args == [False]           # 但坏连接照丢

    async def test_a_failure_that_leaves_the_connection_healthy_is_not_retried(self, monkeypatch):
        """连接还好好的 → 错就是语句自己的错,原样抛(重试治不了语法错)。"""
        conn = FakeConn(error=RuntimeError("syntax error"))
        adapter, driver = make_adapter(monkeypatch, driver=FakeDriver([conn]))
        await adapter.connect()

        with pytest.raises(SQLExecutionError):
            await adapter.execute("SELECT * FORM t")

        assert len(driver.connect_calls) == 1


class TestReadStatementClassification:
    """哪些语句可以在新连接上重发(**纯函数**,判据与理由见适配器常量)。"""

    READ = [
        "SELECT 1",
        "select * from t",
        "  \nSELECT 1",
        "SHOW TABLES",
        "DESCRIBE t",
        "DESC t",
        "EXPLAIN SELECT 1",
        "/* hint */ SELECT 1",
        "-- note\nSELECT 1",
    ]

    NOT_READ = [
        "",                                       # 空串:取不到语句头
        "WITH x AS (SELECT 1) SELECT * FROM x",   # CTE 可以冠在写语句前面
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET a = 1",
        "DELETE FROM t",
        "CREATE TABLE t (a INT)",
        "MERGE INTO t USING s ON t.id = s.id",
        "SELECT seq.NEXTVAL",                     # 读外形、写副作用(推进序列)
        "/* unclosed SELECT 1",                   # 注释没闭合 → 按不重试处理
        "-- unclosed",
    ]

    @pytest.mark.parametrize("sql", READ)
    def test_idempotent_reads(self, sql):
        assert _is_read_statement(sql) is True

    @pytest.mark.parametrize("sql", NOT_READ)
    def test_everything_else_stays_unretried(self, sql):
        assert _is_read_statement(sql) is False


# ── 终止(§7.3 / I4)────────────────────────────────────


class TestTermination:
    async def test_nothing_in_flight_reports_true_and_sends_nothing(self, monkeypatch):
        """「本就无可取消」是 ``True`` —— 与基类契约一致(不是"没发出去")。"""
        conn = FakeConn()
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver([conn]))
        await adapter.connect()

        assert await adapter.interrupt() is True
        assert conn.aborts == []

    async def test_a_cancelled_query_is_aborted_by_its_own_request_id(self, monkeypatch):
        """取消栈上那次 ``interrupt()`` 必须打中**本任务正在飞**的那条查询:

        * abort 带的是**我们注入的那个 requestId**(假连接没有 sfqid 可给 ——
          与真驱动同一形状:查询在飞时它还不存在);
        * abort 带的是那条语句的原文(驱动自己的取消接口要求两样都给)。
        """
        release = threading.Event()
        conn = FakeConn(blocking=release)
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver([conn]))
        await adapter.connect()

        task = asyncio.create_task(adapter.execute("SELECT sleep(30)"))
        while not conn.statements:
            await asyncio.sleep(0.01)
        sql, params = conn.statements[0]

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()

        assert len(conn.aborts) == 1
        aborted_sql, aborted_id = conn.aborts[0]
        assert aborted_sql == sql
        assert str(aborted_id) == params["requestId"]   # 打的就是注入的那个 id

    async def test_a_rejected_abort_is_not_reported_as_sent_when_asked_later(
        self, monkeypatch, caplog,
    ):
        """**「服务端拒了 abort」不能因为「后来没东西可杀」变成成功。**

        超时路径上 ``interrupt()`` 被取消解栈调过一次(结果没人接),而
        ``QueryTerminator`` 是**之后**才来问的 —— 那时 ``_inflight`` 已在
        ``finally`` 里清空。不记结果,第二次只能回一个干净利落的 ``True``,
        折成证据里的 ``kill_sent``:查询还在跑,答案写着"已发出终止指令"。

        走 ``asyncio.wait_for`` 而不是 ``create_task`` 是刻意的 —— 3.12 里
        ``wait_for`` 用 ``async with timeout`` 在**同一任务**里跑,与节点里的
        调用形态逐字一致,连"谁来问"这个任务身份都不差。
        """
        release = threading.Event()
        conn = FakeConn(blocking=release, abort_result={"success": False})
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver([conn]))
        await adapter.connect()

        with caplog.at_level(logging.WARNING):
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    adapter.execute("SELECT sleep(30)"), timeout=0.05,
                )
        release.set()

        assert await adapter.interrupt() is False, "把被拒的终止报成了成功"
        assert len(conn.aborts) == 1, "同一条查询只终止一次(§10 不重试 kill)"
        assert any("not accepted" in r.message for r in caplog.records), (
            "被拒的 abort 必须留下 WARNING —— 静默失败在日志里查不到原因"
        )

    async def test_an_abort_that_raises_is_a_warning_not_an_exception(
        self, monkeypatch, caplog,
    ):
        """abort 发不出去:取消照旧解栈(仍抛 CancelledError),但人得看得见。"""
        release = threading.Event()
        conn = FakeConn(blocking=release, abort_error=RuntimeError("abort denied"))
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver([conn]))
        await adapter.connect()

        with caplog.at_level(logging.WARNING):
            task = asyncio.create_task(adapter.execute("SELECT sleep(30)"))
            while not conn.statements:
                await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        release.set()

        assert any("abort denied" in r.message for r in caplog.records)
        assert len(conn.aborts) == 1, "同一条查询只终止一次(§10)"
        # 结论记在**发起这条查询的任务**上:之后同一个任务来问才会拿到它
        # (真实管线里 ``QueryTerminator`` 与 ``execute`` 同任务,见上面那条
        # wait_for 用例)。换一个任务来问 → 本任务名下没有在飞的查询 → True:
        # 这正是"只杀自己"的另一半,不是"这次 abort 成功了"。
        assert getattr(task, sf_module._INTERRUPT_OUTCOME_ATTR) is False
        assert await adapter.interrupt() is True

    async def test_an_abort_that_hangs_is_bounded_by_the_interrupt_timeout(
        self, monkeypatch,
    ):
        """abort 卡住 → ``INTERRUPT_TIMEOUT_S`` 后报 False,**绝不悬挂**终止路径。"""
        monkeypatch.setattr(sf_module, "INTERRUPT_TIMEOUT_S", 0.05)

        class _SlowConn(FakeConn):
            def _cancel_query(self, sql, request_id):
                self.aborts.append((sql, request_id))
                threading.Event().wait(timeout=0.5)   # 比界长,但不拖整轮
                return {"success": True}

        conn = _SlowConn(blocking=threading.Event())
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver([conn]))
        adapter._inflight[asyncio.current_task()] = ("SELECT sleep(30)", uuid.uuid4())

        assert await adapter.interrupt() is False

    async def test_a_new_query_forgets_the_previous_outcome(self, monkeypatch):
        """上一次终止的结论属于上一条查询 —— 新查询开始必须作废它。

        不清理的话,同一任务里重试第二条查询时 ``interrupt()`` 会拿旧结论
        回话:一条正在飞的查询会因为"上一条 abort 被拒"被判死刑。
        """
        release = threading.Event()
        conn = FakeConn(blocking=release, abort_result={"success": False},
                        default_rows=[[2]], default_description=[("X", 0)])
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver([conn]))
        await adapter.connect()

        # 第一条:超时取消 → abort 被拒 → False 被记在本任务上
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(adapter.execute("SELECT sleep(30)"), timeout=0.05)
        assert await adapter.interrupt() is False
        release.set()

        # 第二条:新查询一开始就作废旧结论 → 跑完后再问是"无可取消"的 True
        assert (await adapter.execute("SELECT 2")).rows == [[2]]
        assert await adapter.interrupt() is True


# ── 内省 ─────────────────────────────────────────────────


class TestGetSchema:
    @staticmethod
    def _conn(*, events=None, rows=None):
        return FakeConn(scripted=[
            (None, events),   # TABLES:description 不参与(适配器只用行)
            (None, rows),     # COLUMNS
        ])

    async def test_reads_information_schema_and_groups_columns_by_table(self, monkeypatch):
        conn = self._conn(
            events=[("STUDENTS", 5), ("LOYAN_VIEW", None), ("ZEROED", 0)],
            rows=[
                ("STUDENTS", "ID", "NUMBER", "NO"),
                ("STUDENTS", "NAME", "TEXT", "YES"),
            ],
        )
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver([conn]))
        await adapter.connect()

        schema = await adapter.get_schema()

        by_name = {t.name: t for t in schema.tables}
        assert set(by_name) == {"STUDENTS", "LOYAN_VIEW", "ZEROED"}
        assert by_name["STUDENTS"].row_count_estimate == 5
        assert by_name["LOYAN_VIEW"].row_count_estimate is None   # NULL(视图/无统计)
        assert by_name["ZEROED"].row_count_estimate is None       # 0 不是依据
        assert by_name["LOYAN_VIEW"].columns == []

        id_col = next(c for c in by_name["STUDENTS"].columns if c.name == "ID")
        assert id_col.nullable is False
        # 雪花的约束**不强制** —— 声明出来的主键不是保证,一律不报
        assert id_col.primary_key is False
        name_col = next(c for c in by_name["STUDENTS"].columns if c.name == "NAME")
        assert name_col.nullable is True
        assert by_name["STUDENTS"].schema == "PUBLIC"

        # 两次往返:TABLES 带 ROW_COUNT + COLUMNS 一次取全(不逐表查)
        assert len(conn.statements) == 2
        tables_sql, columns_sql = (sql for sql, _ in conn.statements)
        assert "FIN.INFORMATION_SCHEMA.TABLES" in tables_sql
        assert "FIN.INFORMATION_SCHEMA.COLUMNS" in columns_sql
        # 服务端把未加引号标识符折成大写 —— 比较字面量跟着折
        assert "TABLE_SCHEMA = 'PUBLIC'" in tables_sql
        assert "ORDER BY TABLE_NAME, ORDINAL_POSITION" in columns_sql

    async def test_a_schema_name_that_is_not_a_plain_identifier_is_refused(self, monkeypatch):
        """库/模式名要拼进 SQL(标识符没法参数化)→ 形状不对就拒绝,不转义。

        加引号的雪花标识符大小写敏感,转义等于给这个名字第二种解释 —— 而它只有
        一种正当解释。
        """
        for bad in ("PUBLIC'; DROP TABLE X --", "has space", "9STARTS_WITH_DIGIT"):
            conn = FakeConn()
            adapter, _ = make_adapter(
                monkeypatch, driver=FakeDriver([conn]),
                config={**DEFAULT_CONFIG, "schema": bad},
            )
            await adapter.connect()
            with pytest.raises(DatasourceError, match="not a plain identifier"):
                await adapter.get_schema()
            assert conn.statements == [], "被拒的名字绝不能到线上"

    async def test_a_missing_name_is_a_config_error(self, monkeypatch):
        conn = FakeConn()
        adapter, _ = make_adapter(
            monkeypatch, driver=FakeDriver([conn]),
            config={k: v for k, v in DEFAULT_CONFIG.items() if k != "database"},
        )
        await adapter.connect()
        with pytest.raises(DatasourceError, match="database"):
            await adapter.get_schema()
        assert conn.statements == []

    async def test_introspection_failure_wraps_datasource_error(self, monkeypatch):
        conn = FakeConn(error=RuntimeError("insufficient privileges"))
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver([conn]))
        await adapter.connect()
        with pytest.raises(DatasourceError, match="insufficient privileges"):
            await adapter.get_schema()

    async def test_get_schema_before_connect_raises(self, monkeypatch):
        adapter, _ = make_adapter(monkeypatch)
        with pytest.raises(DatasourceError, match="Not connected"):
            await adapter.get_schema()


class TestDeclarations:
    async def test_capabilities(self, monkeypatch):
        adapter, _ = make_adapter(monkeypatch)
        caps = await adapter.get_capabilities()
        assert caps.dialect == "snowflake"
        assert caps.supports_cte is True
        assert caps.supports_window_functions is True
        assert caps.supports_transactions is True
        assert caps.supports_json_type is True   # VARIANT

    async def test_profile_capabilities_declare_only_row_count(self, monkeypatch):
        """bytes / last_modified 在雪花上没有可验证的对等口径 —— 不声明就是
        不替它们背书(与 Doris 收窄 MySQL 同一条纪律)。"""
        adapter, _ = make_adapter(monkeypatch)
        assert adapter.profile_capabilities == frozenset({"row_count"})

    async def test_the_readonly_probe_is_honestly_unverifiable(self, monkeypatch):
        """这一版**不写**专属只读探测:基类默认如实报「未验证」。

        写一个"查 ACCOUNT_USAGE 权限"的探测需要它在真云仓上验证过 —— 没验过的
        探测比没有探测更糟:``verified=True`` 是一句替边界背书的话。
        """
        conn = FakeConn()
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver([conn]))
        await adapter.connect()

        probe = await adapter.probe_readonly()
        assert probe.verified is None
        assert probe.basis == BASIS_UNVERIFIABLE
        assert conn.statements == []   # 探测没有偷偷发查询

    def test_dialect_static(self):
        assert SnowflakeAdapter.dialect() == "snowflake"


# ── 集成(SNOWFLAKE_TEST_URL,未设则跳过;CI 不跑)──────────


@pytest.mark.integration
class TestSnowflakeIntegration:
    """**本地无云仓,服务端语义只能在真环境验** —— 设了 ``SNOWFLAKE_TEST_URL``
    才会跑。这里只做最小闭环(连上 → 查一条 → 内省一次),验的是「发出去的形状
    在真服务端被接受」;终止与超时的服务端语义由人工在真环境按适配器 docstring
    里那张单子验。
    """

    @pytest.fixture
    async def snowflake_adapter(self):
        url = os.environ.get("SNOWFLAKE_TEST_URL")
        if not url:
            pytest.skip("SNOWFLAKE_TEST_URL not set")
        from trove.services.datasource.urls import parse_datasource_url

        cfg = parse_datasource_url(url)
        adapter = SnowflakeAdapter(name="integration", config={
            **cfg.connection_params, **cfg.credentials,
        })
        await adapter.connect()
        try:
            yield adapter
        finally:
            await adapter.disconnect()

    async def test_full_lifecycle(self, snowflake_adapter):
        result = await snowflake_adapter.execute("SELECT 1 AS ONE")
        assert result.columns == ["ONE"]
        assert result.rows == [[1]]

        schema = await snowflake_adapter.get_schema()
        assert schema.tables, "模式里应当有表;空模式说明库/模式名没对上"
