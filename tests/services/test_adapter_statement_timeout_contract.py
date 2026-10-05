"""适配器的 **DB 侧语句超时** 声明必须与代码一致(设计 §10 / I4)。

与 ``test_adapter_interrupt_contract.py`` 同一条纪律 —— 那条钉「能不能发终止」,
这条钉「库里的那条闸是不是真的存在」。三个错法都很安静:

* **声明 True 却没实现** → 连接上没有任何库侧界,而大家都以为有;
  「进程被杀 / 事件循环卡死,查询还在库里跑」这个要防的失败模式原样成立。
* **声明 False 却发了语句** → 在一个语义未验证的引擎上(Doris)塞了一条它
  未必认识的语句;轻则白费一次往返,重则整条连接被拒。
* **配置写错静默变成"关掉"** → 运维以为配了 5s 的上界,实际是没有上界 ——
  这是三态解析(缺省 / ≤0 / 非数)专门防的那一件事。

还有一条不在这条纪律里、但同样会说谎的量:库侧缺省界与**应用侧预算**的大小
关系。应用侧才是主闸(超时 → 取消 → ``QueryTerminator`` 的 KILL 证据链);
库侧更小或同值,主闸的证据链就永远没有机会生成,故障归因会从「Trove 杀了这条
查询」悄悄变成「库自己超时了」。这条关系也在这里钉住。
"""

from __future__ import annotations

import logging

import pytest

from trove.core.config import BudgetConfig
from trove.services.datasource.adapters.base import (
    DEFAULT_STATEMENT_TIMEOUT_MS,
    DatabaseAdapter,
)
from trove.services.datasource.adapters.clickhouse import ClickHouseAdapter
from trove.services.datasource.adapters.doris import DorisAdapter
from trove.services.datasource.adapters.mysql import MySQLAdapter
from trove.services.datasource.adapters.postgres import PostgresAdapter
from trove.services.datasource.adapters.snowflake import SnowflakeAdapter
from trove.services.datasource.adapters.sqlite import SQLiteAdapter
from trove.services.datasource.registry import _ADAPTER_REGISTRY

#: 今天的预期矩阵。True 的四个各有**验证过**的机制(见各自声明处的注释);
#: False 的四个是**刻意的**,变化必须由这条测试逼一次回答。
_DECLARED = {
    "mysql": True,       # SET SESSION max_execution_time / max_statement_time(MariaDB)
    "postgres": True,    # 连接参数 options=-c statement_timeout=<ms>
    "clickhouse": True,  # 请求级 settings.max_execution_time(秒)
    "snowflake": True,   # 登录参数 session_parameters.STATEMENT_TIMEOUT_IN_SECONDS(秒)
    "sqlite": False,     # 进程内引擎,无可保护的服务端对象
    "duckdb": False,     # 同上,且没有等价的会话级设置
    "doris": False,      # 会话超时语句语义未在本仓验证(收窄 MySQL 的 True)
    "bigquery": False,   # 无会话/连接级时长机制;逐 job 的 job_timeout_ms 是另一条
                         # 通道且服务端语义未验证 —— 一个字都不发(收窄的诚实版)
}


class TestTheDeclarationMatchesTheCode:
    def test_declared_matrix_is_the_expected_one(self):
        actual = {
            dialect: cls.supports_statement_timeout
            for dialect, cls in _ADAPTER_REGISTRY.items()
        }
        assert actual == _DECLARED

    def test_declared_true_means_a_real_hook_override(self):
        """声明 True ⇔ 至少一个钩子不是基类空实现 —— 两侧都写才算数。"""
        for dialect, cls in _ADAPTER_REGISTRY.items():
            if not cls.supports_statement_timeout:
                continue
            overridden = (
                cls._statement_timeout_connect_kwargs
                is not DatabaseAdapter._statement_timeout_connect_kwargs
                or cls._apply_statement_timeout
                is not DatabaseAdapter._apply_statement_timeout
            )
            assert overridden, (
                f"{dialect}: 声明支持 DB 侧超时,但两个钩子都是基类空实现"
            )

    def test_the_floor_is_above_the_app_side_budget(self):
        """库侧缺省界必须**大于**应用侧预算:主闸先手,库侧只兜底。"""
        assert DEFAULT_STATEMENT_TIMEOUT_MS > BudgetConfig().timeout_ms


class TestUndeclaredDialectsSendNothing:
    """声明 False = **一个字都不发** —— 连显式配置了 ``statement_timeout_ms``
    也一样(能力先于配置:没有机制可发,配置就不该把它变出来)。"""

    @pytest.mark.parametrize(
        "dialect", [d for d, ok in _DECLARED.items() if not ok],
    )
    async def test_both_channels_stay_silent(self, dialect):
        adapter = _ADAPTER_REGISTRY[dialect](
            name=dialect, config={"statement_timeout_ms": 5_000},
        )
        assert adapter.statement_timeout_ms() is None
        assert adapter.statement_timeout_connect_kwargs() == {}
        assert await adapter.apply_statement_timeout() is False

    async def test_doris_never_reaches_the_inherited_mysql_hook(self):
        """Doris 继承了 MySQL 的 ``_apply_statement_timeout`` —— 公开入口的短路
        必须发生在它**之前**,否则 ``SET SESSION max_execution_time`` 就发到
        一个语义未验证的引擎上了。"""
        class _ExplodingConn:
            touched = False

            async def cursor(self):
                self.touched = True
                raise AssertionError("Doris 不该走到 MySQL 的钩子里")

        adapter = DorisAdapter(name="d", config={"statement_timeout_ms": 5_000})
        conn = _ExplodingConn()
        adapter._conn = conn
        assert await adapter.apply_statement_timeout() is False
        assert conn.touched is False


class TestTimeoutParsing:
    """连接参数 ``statement_timeout_ms`` 的三态:缺省 / ≤0 / 非数。"""

    def _mysql(self, **cfg) -> MySQLAdapter:
        return MySQLAdapter(name="m", config=cfg)

    def test_absent_uses_the_global_default(self):
        assert self._mysql().statement_timeout_ms() == DEFAULT_STATEMENT_TIMEOUT_MS

    def test_explicit_value_wins(self):
        assert self._mysql(statement_timeout_ms=5_000).statement_timeout_ms() == 5_000

    def test_numeric_string_is_accepted(self):
        assert self._mysql(statement_timeout_ms="7000").statement_timeout_ms() == 7_000

    @pytest.mark.parametrize("raw", [0, -1, "0"])
    def test_non_positive_is_an_explicit_off(self, raw):
        assert self._mysql(statement_timeout_ms=raw).statement_timeout_ms() is None

    @pytest.mark.parametrize("raw", [None, "abc", [1]])
    def test_garbage_falls_back_to_the_default_not_off(self, raw):
        """写错的配置不许静默变成"关掉"——那正是配错时最不该拿到的结果。"""
        cfg = {} if raw is None else {"statement_timeout_ms": raw}
        assert self._mysql(**cfg).statement_timeout_ms() == DEFAULT_STATEMENT_TIMEOUT_MS

    def test_undeclared_dialect_reports_none_whatever_the_config(self):
        adapter = SQLiteAdapter(name="s", config={"statement_timeout_ms": 5_000})
        assert adapter.statement_timeout_ms() is None


# ── 各方言的发射形状 ──────────────────────────────────────────────


class _FakeCursor:
    def __init__(self, log: list[str]):
        self._log = log

    async def execute(self, sql, *args):
        self._log.append(sql)

    async def fetchone(self):
        return ("8.0.36",)

    async def fetchall(self):
        return []

    async def close(self):
        ...


class _FakeConn:
    """够 mysql 的建连 / ``_apply_statement_timeout`` / ``_ping_reconnect`` 用。"""

    def __init__(self, cursor_fails: bool = False):
        self.executed: list[str] = []
        self.pings = 0
        self._cursor_fails = cursor_fails

    async def cursor(self):
        if self._cursor_fails:
            raise RuntimeError("cursor refused")
        return _FakeCursor(self.executed)

    async def ping(self, reconnect: bool = True):
        self.pings += 1


class TestMySQL:
    async def test_arms_the_session_variable(self):
        adapter = MySQLAdapter(name="m", config={})
        adapter._conn = _FakeConn()
        assert await adapter.apply_statement_timeout() is True
        assert adapter._conn.executed == ["SET SESSION max_execution_time = 60000"]

    async def test_explicit_off_sends_nothing(self):
        adapter = MySQLAdapter(name="m", config={"statement_timeout_ms": 0})
        adapter._conn = _FakeConn()
        assert await adapter.apply_statement_timeout() is False
        assert adapter._conn.executed == []

    async def test_every_ping_rearms_the_session_variable(self):
        """ping 可能悄悄重连并带走会话变量(且无法从返回值区分)。"""
        adapter = MySQLAdapter(name="m", config={})
        adapter._conn = _FakeConn()
        await adapter._ping_reconnect()
        assert adapter._conn.pings == 1
        assert adapter._conn.executed == ["SET SESSION max_execution_time = 60000"]

    async def test_a_refusing_cursor_is_reported_not_raised(self, caplog):
        """旧引擎认不出这个变量名 —— 降级可以,静默不行(warning + False)。"""
        adapter = MySQLAdapter(name="m", config={})
        adapter._conn = _FakeConn(cursor_fails=True)
        with caplog.at_level(logging.WARNING):
            assert await adapter.apply_statement_timeout() is False
        assert any("max_execution_time" in r.message for r in caplog.records)

    async def test_no_connection_is_reported_not_raised(self):
        adapter = MySQLAdapter(name="m", config={})
        assert await adapter.apply_statement_timeout() is False

    async def test_mariadb_gets_its_own_variable_and_unit(self):
        """MariaDB **不认** ``max_execution_time``(MySQL 独有):它的是
        ``max_statement_time``,且单位是**秒**。60000ms 发成 60,不是 60000 ——
        单位照搬就是一条 16 小时的上界。"""
        adapter = MySQLAdapter(name="m", config={})
        adapter._conn = _FakeConn()
        adapter._server_version = "10.6.12-MariaDB-1:10.6.12+maria~ubu2004"
        assert await adapter.apply_statement_timeout() is True
        assert adapter._conn.executed == ["SET SESSION max_statement_time = 60"]

    def test_mariadb_sub_second_keeps_the_fraction(self):
        """400ms → 0.4 秒。折成整数秒就是「一条 0 秒的上界」= 关掉。"""
        adapter = MySQLAdapter(name="m", config={})
        adapter._server_version = "5.5.5-10.11.6-MariaDB"  # 旧客户端协议串
        assert adapter._statement_timeout_sql(400) == (
            "SET SESSION max_statement_time = 0.4"
        )

    def test_an_unknown_engine_falls_back_to_the_mysql_form(self):
        """版本探测失败(空串)不许猜 MariaDB:猜错的代价只是一条 WARNING,
        但**用错变量的"成功"**会让日志说「已装闸」而库里其实没有。"""
        adapter = MySQLAdapter(name="m", config={})
        assert adapter._statement_timeout_sql(60_000) == (
            "SET SESSION max_execution_time = 60000"
        )

    async def test_connect_arms_the_timeout_after_probing_the_version(
        self, monkeypatch,
    ):
        """建连路径:先探测版本再装闸 —— 变量名依赖引擎(MySQL vs MariaDB)。
        探测是服务端常量查询、不扫数据,先跑它的暴露可以忽略。"""
        conn = _FakeConn()

        class _FakeDriver:
            async def connect(self, **kwargs):
                return conn

        monkeypatch.setattr(
            MySQLAdapter, "_get_driver", classmethod(lambda cls: _FakeDriver()),
        )
        adapter = MySQLAdapter(name="m", config={})
        await adapter.connect()
        # _FakeCursor.fetchone → ("8.0.36",) → 非 MariaDB → MySQL 形态
        assert conn.executed == [
            "SELECT VERSION()",
            "SET SESSION max_execution_time = 60000",
        ]


class TestPostgres:
    def test_statement_timeout_travels_as_a_connect_option(self):
        adapter = PostgresAdapter(name="p", config={})
        assert adapter.statement_timeout_connect_kwargs() == {
            "options": "-c statement_timeout=60000",
        }

    def test_explicit_value_wins(self):
        adapter = PostgresAdapter(name="p", config={"statement_timeout_ms": 5_000})
        assert adapter.statement_timeout_connect_kwargs() == {
            "options": "-c statement_timeout=5000",
        }

    def test_explicit_off_adds_no_options(self):
        adapter = PostgresAdapter(name="p", config={"statement_timeout_ms": -1})
        assert adapter.statement_timeout_connect_kwargs() == {}


class TestClickHouse:
    def test_settings_ride_the_shared_connect_kwargs(self):
        """旁路 KILL 连接与原连接同参 —— setting 折在 ``_connect_kwargs`` 里,
        两边自动一致(否则是连到另一个库上去杀)。"""
        adapter = ClickHouseAdapter(name="c", config={})
        kwargs = adapter._connect_kwargs()
        assert kwargs["settings"] == {"max_execution_time": 60}
        assert kwargs["host"] == "127.0.0.1"  # 原有参数没被 setting 挤掉

    def test_sub_second_budget_never_folds_to_zero(self):
        """``ms < 1000`` 折成 0 就是「无限制」—— 静默失效的闸比没有闸更糟。"""
        adapter = ClickHouseAdapter(name="c", config={"statement_timeout_ms": 400})
        assert adapter._connect_kwargs()["settings"] == {"max_execution_time": 1}

    def test_explicit_off_adds_no_settings_key(self):
        adapter = ClickHouseAdapter(name="c", config={"statement_timeout_ms": 0})
        assert "settings" not in adapter._connect_kwargs()


class TestSnowflake:
    def test_settings_ride_the_shared_connect_kwargs(self):
        """闸折在 ``_connect_kwargs`` 里,与登录同一个请求 —— 不存在「连上了
        但闸还没装」的窗口,重连也自动重装(不需要 MySQL 那种 ping 重装)。"""
        adapter = SnowflakeAdapter(name="s", config={"account": "acct"})
        kwargs = adapter._connect_kwargs()
        assert kwargs["session_parameters"] == {"STATEMENT_TIMEOUT_IN_SECONDS": 60}
        assert kwargs["account"] == "acct"  # 原有参数没被 setting 挤掉

    def test_sub_second_budget_never_folds_to_zero(self):
        """``ms < 1000`` 折成 0 不是「关」—— 雪花文档里 0 = **按最大值(7 天)**
        执行,折零等于把一道闸变成一句空话。"""
        adapter = SnowflakeAdapter(name="s", config={
            "account": "acct", "statement_timeout_ms": 400,
        })
        assert adapter._connect_kwargs()["session_parameters"] == {
            "STATEMENT_TIMEOUT_IN_SECONDS": 1,
        }

    def test_explicit_off_adds_no_settings_key(self):
        adapter = SnowflakeAdapter(name="s", config={
            "account": "acct", "statement_timeout_ms": 0,
        })
        assert "session_parameters" not in adapter._connect_kwargs()

    async def test_connect_sends_the_settings_with_the_login_request(
        self, monkeypatch,
    ):
        """建连路径:session_parameters 必须真的进 ``snowflake.connect`` ——
        单测钩子形状只证明"算得出",这条证明"发得出去"。"""
        seen: dict = {}

        class _FakeConn:
            def is_closed(self):
                return False

        class _FakeDriver:
            @staticmethod
            def connect(**kwargs):
                seen.update(kwargs)
                return _FakeConn()

        monkeypatch.setattr(
            "trove.services.datasource.adapters.snowflake._get_driver",
            lambda: _FakeDriver,
        )
        adapter = SnowflakeAdapter(name="s", config={
            "account": "acct", "database": "DB", "schema": "PUBLIC",
        })
        await adapter.connect()
        assert seen["session_parameters"] == {"STATEMENT_TIMEOUT_IN_SECONDS": 60}
        assert seen["account"] == "acct"
        assert seen["schema"] == "PUBLIC"


class TestTheBaseContract:
    async def test_base_declares_nothing_and_emits_nothing(self):
        """基类不声明能力:三个入口都给「没有」,而不是替不存在的机制背书。"""

        class Bare(DatabaseAdapter):
            @staticmethod
            def dialect() -> str: return "bare"
            async def connect(self) -> None: ...
            async def disconnect(self) -> None: ...
            async def execute(self, sql): ...
            async def get_schema(self): ...
            async def get_capabilities(self): ...

        adapter = Bare(name="bare", config={"statement_timeout_ms": 5_000})

        assert adapter.supports_statement_timeout is False
        assert adapter.statement_timeout_ms() is None
        assert adapter.statement_timeout_connect_kwargs() == {}
        assert await adapter.apply_statement_timeout() is False
