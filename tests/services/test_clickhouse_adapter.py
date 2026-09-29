"""ClickHouse adapter tests — unit (fake driver) + integration (CLICKHOUSE_TEST_URL)."""

import asyncio
import logging
import os
import threading
import uuid

import pytest

from trove.core.errors import DatasourceError, SQLExecutionError
from trove.services.datasource.adapters import clickhouse as ch_module
from trove.services.datasource.adapters.clickhouse import ClickHouseAdapter


class FakeResult:
    def __init__(self, column_names, result_rows):
        self.column_names = column_names
        self.result_rows = result_rows


class FakeClient:
    """**照真驱动的契约说话**(不是照我以为的契约)。

    真驱动(clickhouse-connect 1.7.1)在查询路径上只读 ``context.settings``
    (``driver/_backendclient.py:46``);``transport_settings`` 那个参数尽管在
    ``valid_transport_settings`` 里认 ``query_id``,却**根本没被用在查询上**
    —— 实测:``transport_settings={"query_id": q}`` 时服务端 ``currentQueryID()``
    返回的是一个随机 UUID,而 ``settings={"query_id": q}`` 才返回 ``q``。

    所以这个假驱动**只认 settings**,并且把 transport_settings 记下来备查:
    假驱动比真驱动宽容一分,测试就少测一分 —— 这条路径上少测的那一分,
    就是「KILL 发出去、服务端没这个 id、查询照跑、证据却写 kill_sent」。
    """

    def __init__(self, scripted=None, *, blocking=None, command_error=None):
        # scripted: list of FakeResult returned per query() call
        self._scripted = list(scripted or [])
        self._blocking = blocking          # threading.Event: 卡住 query 用
        self._command_error = command_error
        self.queries = []
        self.settings = []
        self.transport_settings = []       # 真驱动不看它,这里留着证明没人该用它
        self.commands = []
        self.command_attempts = 0
        self.closed = False

    def query(self, sql, parameters=None, settings=None, transport_settings=None):
        self.queries.append((sql, parameters))
        self.settings.append(settings)
        self.transport_settings.append(transport_settings)
        if self._blocking is not None:
            self._blocking.wait(timeout=10)
        return self._scripted.pop(0) if self._scripted else FakeResult([], [])

    def command(self, sql, **kwargs):
        # 先记**尝试**再报错:失败的 KILL 不进 self.commands,于是「发了几次」
        # 单看 commands 是看不出来的(§10 的不重试要数的正是次数)
        self.command_attempts += 1
        if self._command_error is not None:
            raise self._command_error
        self.commands.append(sql)
        return None

    def close(self):
        self.closed = True


class FakeDriver:
    def __init__(self, client=None, *, side_clients=None):
        self.client = client or FakeClient()
        # 旁路连接(interrupt 用):同一时刻在跑的 client 正被那条查询占着
        self._side = list(side_clients or [])
        self.get_client_kwargs = None
        self.get_client_calls = []
        self.clients = []

    def get_client(self, **kwargs):
        self.get_client_kwargs = kwargs
        self.get_client_calls.append(kwargs)
        # 第一次是 connect(主 client);之后才是旁路(interrupt 那条)
        if len(self.get_client_calls) == 1:
            client = self.client
        else:
            client = self._side.pop(0) if self._side else self.client
        self.clients.append(client)
        return client


def make_adapter(monkeypatch, driver=None, config=None):
    driver = driver or FakeDriver()
    monkeypatch.setattr(ch_module, "_get_driver", lambda: driver)
    adapter = ClickHouseAdapter(
        name="test",
        config=config or {
            "host": "127.0.0.1", "port": 8123,
            "user": "default", "password": "p", "database": "testdb",
        },
    )
    return adapter, driver


class TestClickHouseAdapter:
    async def test_connect_passes_parsed_params(self, monkeypatch):
        adapter, driver = make_adapter(monkeypatch)
        await adapter.connect()

        assert adapter.is_connected
        kwargs = driver.get_client_kwargs
        assert kwargs["host"] == "127.0.0.1"
        assert kwargs["port"] == 8123
        assert kwargs["username"] == "default"
        assert kwargs["password"] == "p"
        assert kwargs["database"] == "testdb"

    async def test_connect_failure_wraps_datasource_error(self, monkeypatch):
        class BadDriver:
            def get_client(self, **kwargs):
                raise OSError("connection refused")

        monkeypatch.setattr(ch_module, "_get_driver", lambda: BadDriver())
        adapter = ClickHouseAdapter(name="test", config={})
        with pytest.raises(DatasourceError):
            await adapter.connect()
        assert not adapter.is_connected

    async def test_execute_returns_query_result(self, monkeypatch):
        client = FakeClient([FakeResult(["id", "name"], [[1, "a"], [2, "b"]])])
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(client))
        await adapter.connect()

        result = await adapter.execute("SELECT id, name FROM t")
        assert result.columns == ["id", "name"]
        assert result.rows == [[1, "a"], [2, "b"]]
        assert result.row_count == 2
        assert client.queries[0][0] == "SELECT id, name FROM t"

    async def test_execute_error_wraps(self, monkeypatch):
        class ExplodingClient:
            def query(self, sql, parameters=None, settings=None):
                raise RuntimeError("table not found")

        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(ExplodingClient()))
        await adapter.connect()

        with pytest.raises(SQLExecutionError) as exc_info:
            await adapter.execute("SELECT * FROM ghost")
        assert "table not found" in str(exc_info.value)

    async def test_execute_before_connect_raises(self, monkeypatch):
        adapter, _ = make_adapter(monkeypatch)
        with pytest.raises(SQLExecutionError):
            await adapter.execute("SELECT 1")

    async def test_get_schema_introspects_system_tables(self, monkeypatch):
        client = FakeClient([
            FakeResult(["name", "total_rows"], [("events", 1000)]),
            FakeResult(
                ["name", "type", "is_in_primary_key"],
                [("id", "UInt64", 1), ("name", "String", 0)],
            ),
        ])
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(client))
        await adapter.connect()

        schema = await adapter.get_schema()
        assert len(schema.tables) == 1
        table = schema.tables[0]
        assert table.name == "events"
        assert table.row_count_estimate == 1000

        id_col = next(c for c in table.columns if c.name == "id")
        assert id_col.primary_key is True

        name_col = next(c for c in table.columns if c.name == "name")
        assert name_col.primary_key is False

        executed = " ".join(sql for sql, _ in client.queries)
        assert "system.tables" in executed
        assert "system.columns" in executed

    async def test_get_capabilities(self, monkeypatch):
        adapter, _ = make_adapter(monkeypatch)
        caps = await adapter.get_capabilities()
        assert caps.dialect == "clickhouse"
        assert caps.supports_cte is True
        assert caps.supports_window_functions is True
        assert caps.supports_transactions is False
        assert caps.supports_json_type is True

    async def test_dialect_static(self):
        assert ClickHouseAdapter.dialect() == "clickhouse"

    async def test_missing_driver_hint(self, monkeypatch):
        def _missing():
            raise DatasourceError(
                message="clickhouse-connect is not installed — run `uv sync --extra clickhouse`",
                datasource="",
            )

        monkeypatch.setattr(ch_module, "_get_driver", _missing)
        adapter = ClickHouseAdapter(name="test", config={})
        with pytest.raises(DatasourceError) as exc_info:
            await adapter.connect()
        assert "uv sync --extra clickhouse" in str(exc_info.value)

    async def test_disconnect_closes_client(self, monkeypatch):
        client = FakeClient()
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(client))
        await adapter.connect()
        await adapter.disconnect()
        assert not adapter.is_connected
        assert client.closed is True


class TestClickHouseTermination:
    """ClickHouse 的终止(设计 §7.3 的 ``KILL QUERY WHERE query_id = ...``)。

    这个方言在 P4 之前**一行都没有** —— 它用同步驱动包在 ``to_thread`` 里,
    取消只把**等待**收回:工作线程照跑,服务端查询照跑。要真停掉,只能从
    旁路连接按 ``query_id`` 打 KILL(在跑的那个 client 发不出 KILL,与 MySQL
    的旁路连接同构)。
    """

    async def test_a_query_carries_an_id_the_server_can_be_asked_to_kill(
        self, monkeypatch,
    ):
        """没有 query_id 就没有精确匹配的对象 —— 而 R4 要求的正是精确匹配。"""
        client = FakeClient([FakeResult(["x"], [[1]])])
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(client))
        await adapter.connect()

        await adapter.execute("SELECT 1")

        sent = client.settings[0]
        assert sent and sent["query_id"], (
            "执行必须用 settings= 带上 query_id —— transport_settings= 真驱动"
            "在查询路径上不看(实测),那条 KILL 会打在一个服务端不认识的 id 上"
        )
        assert not client.transport_settings[0], "transport_settings 不是这条路"

    async def test_a_cancelled_query_is_killed_by_its_own_id(self, monkeypatch):
        release = threading.Event()
        busy = FakeClient(blocking=release)
        side = FakeClient()
        adapter, _ = make_adapter(
            monkeypatch, driver=FakeDriver(busy, side_clients=[side]),
        )
        await adapter.connect()

        task = asyncio.create_task(adapter.execute("SELECT sleep(30)"))
        while not busy.settings:  # 等查询真的发出去
            await asyncio.sleep(0.01)
        query_id = busy.settings[0]["query_id"]

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()

        assert side.commands == [f"KILL QUERY WHERE query_id = '{query_id}'"]
        # 在跑的那个 client 发不出 KILL(它正被那条查询占着)
        assert busy.commands == []
        # 旁路连接用完就关:超时路径上漏连接比漏一次 kill 更难查
        assert side.closed is True

    async def test_a_query_that_finished_leaves_nothing_to_kill(self, monkeypatch):
        """用完就清:``query_id`` 留到下一次执行,就会杀到别人的查询(R4)。"""
        side = FakeClient()
        client = FakeClient([FakeResult(["x"], [[1]])])
        adapter, driver = make_adapter(
            monkeypatch, driver=FakeDriver(client, side_clients=[side]),
        )
        await adapter.connect()
        await adapter.execute("SELECT 1")

        assert await adapter.interrupt() is True
        assert side.commands == []
        assert len(driver.get_client_calls) == 1  # 只有 connect 建过 client

    async def test_a_kill_that_cannot_be_sent_is_a_warning_not_an_exception(
        self, monkeypatch, caplog,
    ):
        """终止发不出去**必须留下 WARNING**。

        这条用例钉的是 P4 要修的那个根因:存量四个适配器把终止失败吞进
        ``logger.debug``,于是「发出去了没有」在日志和答案里都查不到。
        取消解栈照旧不能被打断(仍抛 CancelledError),但人得看得见。
        """
        release = threading.Event()
        busy = FakeClient(blocking=release)
        side = FakeClient(command_error=RuntimeError("KILL QUERY denied"))
        adapter, _ = make_adapter(
            monkeypatch, driver=FakeDriver(busy, side_clients=[side]),
        )
        await adapter.connect()

        task = asyncio.create_task(adapter.execute("SELECT sleep(30)"))
        while not busy.settings:
            await asyncio.sleep(0.01)

        with caplog.at_level(logging.WARNING):
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        release.set()

        assert side.commands == []
        assert any("KILL QUERY denied" in r.message for r in caplog.records)

    async def test_a_failed_kill_is_not_reported_as_sent_when_asked_later(
        self, monkeypatch, caplog,
    ):
        """**「杀失败了」不能因为「后来没东西可杀」而变成成功。**

        这是 P4 里最容易被自己骗过去的一格,而且**单看哪一层都看不出来**:

        * 取消解栈里那次 ``interrupt()`` 的结果没人接(它只是收尾);
        * 等到 ``QueryTerminator`` 来问,``_inflight`` 已经在 ``finally`` 里清空了
          —— 于是「本任务名下没有在飞的查询」被回成一个干净利落的 ``True``,
          折成证据里的 ``kill_sent``。

        也就是说,**证据恰好在它唯一想覆盖的那个场景里说谎**:KILL 被拒/连接建
        不起来时,答案写着「已发出终止指令」,而服务端那条查询还在跑。

        这里走 ``asyncio.wait_for``(而不是 ``create_task``)是刻意的 ——
        ``wait_for`` 在 Python 3.12 用 ``async with timeout`` 在同一任务里跑,
        与节点里的调用形态逐字一致,连「谁来问」这个任务身份都不差。
        """
        release = threading.Event()
        busy = FakeClient(blocking=release)
        side = FakeClient(command_error=RuntimeError("KILL QUERY denied"))
        adapter, _ = make_adapter(
            monkeypatch, driver=FakeDriver(busy, side_clients=[side]),
        )
        await adapter.connect()

        with caplog.at_level(logging.WARNING):
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    adapter.execute("SELECT sleep(30)"), timeout=0.05,
                )
        release.set()

        assert await adapter.interrupt() is False, "把失败的终止报成了成功"
        assert side.command_attempts == 1, "同一条查询只终止一次(§10)"


# ── Integration tests (CLICKHOUSE_TEST_URL, skipped when unset) ──


@pytest.mark.integration
class TestClickHouseIntegration:
    @pytest.fixture
    async def clickhouse_env(self):
        url = os.environ.get("CLICKHOUSE_TEST_URL")
        if not url:
            pytest.skip("CLICKHOUSE_TEST_URL not set")
        from trove.services.datasource.urls import parse_datasource_url
        return parse_datasource_url(url)

    # ── 终止(P4)─────────────────────────────────────────

    #: 跑得足够久,久到「取消时它还在飞」是确定的;被 KILL 时不会跑完。
    LONG_SQL = "SELECT sum(number) FROM numbers_mt(200000000000)"

    @staticmethod
    def _running(admin) -> list[tuple]:
        """服务端此刻在跑的长查询。

        ``NOT LIKE '%system.processes%'`` 不是装饰:轮询语句自己的文本里就带着
        上面那个 ``LIKE`` 模式,不排掉它,这条查询会**一直看见自己** —— 于是
        「还在跑」永远成立,用例变成一条恒假的断言(这不是假想:第一版就这么
        写的,失败信息里那个 id 是轮询语句自己的)。
        """
        return admin.query(
            "SELECT query_id FROM system.processes "
            "WHERE query LIKE '%numbers_mt(200000000000)%' "
            "AND query NOT LIKE '%system.processes%'",
        ).result_rows

    async def test_a_killed_query_really_stops_on_the_server(self, clickhouse_env):
        """R4 的实物验证:超时后服务端**真的**不再跑那条查询。

        为什么非连真库不可 —— 这条路上最贵的那个错误,**假驱动看不出来**:
        适配器可以把 ``query_id`` 交给驱动的一个参数,而驱动不拿它做查询。
        假驱动照收不误,单测全绿,真服务端却跑着一条谁也叫不停的长查询,
        而证据上写着 ``kill_sent``。这不是假想:P4 第一版就是这么写的
        (``transport_settings=``),真库上实测服务端看到的是随机 UUID。

        所以这里不认本地记了什么,只认**服务端认不认这个 id**:
        先按 ``system.processes`` 确认在飞查询带的就是适配器手里那个 id,
        再取消,再确认它从表里消失。
        """
        import clickhouse_connect

        params = clickhouse_env.connection_params
        admin = clickhouse_connect.get_client(
            host=params["host"], port=params["port"],
            username=params["user"], password=params["password"],
        )
        adapter = ClickHouseAdapter(name="kill_test", config={**params})
        await adapter.connect()
        try:
            task = asyncio.create_task(adapter.execute(self.LONG_SQL))
            for _ in range(100):  # 等它真的发到服务端(最多 5s)
                await asyncio.sleep(0.05)
                rows = self._running(admin)
                if rows:
                    break
            else:
                pytest.fail("长查询没能在服务端跑起来,这条用例测不到终止")

            server_side_id = rows[0][0]
            # 同一条查询的两头必须是同一个 id —— 这一行就是 R4 的全部依据:
            # 适配器将来要 KILL 的,得正是服务端此刻在跑的那一个。
            assert adapter._inflight[task] == server_side_id, (
                f"适配器手里是 {adapter._inflight.get(task)},服务端在跑的是 "
                f"{server_side_id} —— 照前者发 KILL 谁也打不中"
            )

            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            # 按 **id** 收尾(而不是再按文本认一遍):这里要问的正是「刚才那个
            # 服务端认下的 id,现在还在不在」。
            for _ in range(100):
                await asyncio.sleep(0.05)
                left = admin.query(
                    "SELECT count() FROM system.processes WHERE query_id = %(q)s",
                    parameters={"q": server_side_id},
                ).result_rows
                if left[0][0] == 0:
                    break
            else:
                pytest.fail(
                    f"取消之后服务端还在跑 query_id={server_side_id} —— "
                    "KILL 没打中,或者根本没带上服务端认识的那个 id"
                )
        finally:
            await adapter.disconnect()
            admin.close()

    async def test_full_lifecycle_against_real_clickhouse(self, clickhouse_env):
        import clickhouse_connect

        params = clickhouse_env.connection_params
        test_db = f"trove_test_{uuid.uuid4().hex[:8]}"

        admin = clickhouse_connect.get_client(
            host=params["host"], port=params["port"],
            username=params["user"], password=params["password"],
        )
        try:
            admin.command(f"CREATE DATABASE {test_db}")

            adapter = ClickHouseAdapter(
                name=test_db, config={**params, "database": test_db},
            )
            await adapter.connect()
            try:
                await adapter.execute(
                    "CREATE TABLE students (id UInt64, name String) "
                    "ENGINE = MergeTree ORDER BY id"
                )
                await adapter.execute(
                    "INSERT INTO students VALUES (1, 'Alice'), (2, 'Bob')"
                )

                result = await adapter.execute("SELECT * FROM students ORDER BY id")
                assert result.row_count == 2
                assert result.columns == ["id", "name"]

                schema = await adapter.get_schema()
                table = next(t for t in schema.tables if t.name == "students")
                assert len(table.columns) == 2
            finally:
                await adapter.disconnect()
        finally:
            admin.command(f"DROP DATABASE IF EXISTS {test_db}")
            admin.close()
