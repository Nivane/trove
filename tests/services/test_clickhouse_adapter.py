"""ClickHouse adapter tests — unit (fake driver) + integration (CLICKHOUSE_TEST_URL)."""

import asyncio
import logging
import os
import re
import threading
import uuid

import pytest

from trove.core.errors import DatasourceError, SQLExecutionError
from trove.core.types import BASIS_GRANTS, BASIS_PROBE_FAILED
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

    def __init__(self, scripted=None, *, blocking=None, command_error=None,
                 query_error=None):
        # scripted: list of FakeResult returned per query() call
        self._scripted = list(scripted or [])
        self._blocking = blocking          # threading.Event: 卡住 query 用
        self._command_error = command_error
        self._query_error = query_error    # 查询被拒(权限不足等)用
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
        if self._query_error is not None:
            raise self._query_error
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


def _squash(sql: str) -> str:
    """把 SQL 压成单空格分隔的一行 —— 断结构时不想被换行和缩进绊住。"""
    return " ".join(sql.split())


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


class TestClickHouseReadonlyProbe:
    """只读自检的 ClickHouse 实现(设计 §4 I1)。

    与 MySQL 那份是同一件事的两种方言写法,但 CH 这里多两个**方言特有**的坑,
    各有一条用例钉着:

    * ``system.grants.access_type`` 是 ``Enum16``(**不是 String**)——
      ``upper(access_type)`` 在真库上直接报错(code 43,实测 25.12),所以只能
      拿字面量去比;
    * 权限可以**通过角色**拿到,而 ``system.grants`` 只列出「授给这个用户」和
      「授给这个角色」两种行,角色继承不会自动展开 —— 少了递归展开,一个「通过
      角色拿到 INSERT」的账号会被判成只读,那正是最危险的方向。
    """

    #: 真容器上实测的那一行形状:``clickhouse`` 账号 41 条授权(``GRANT ALL``
    #: 展开后的样子),其中 8 条是写权限。假驱动喂的就是这个数。
    REAL_WRITABLE = (41, 8, "ALTER, CREATE, DROP, INSERT, OPTIMIZE, TRUNCATE, UNDROP TABLE, WRITE")

    #: 这一版**必须认**的写权限:组别名(``WRITE``)+ 细粒度权限 + 父类型本身。
    #: 这里**写死在用例这一侧**,不是去遍历实现里那个常量 —— 遍历它是自问自答:
    #: 把常量收窄成只剩 ``INSERT``,断言照样全绿,而真库上一个只授了 ``TRUNCATE``
    #: 的账号会被判成只读(``verified=true``,替一道不存在的边界背书)。
    #: 「哪些算写得动」是**设计决定**,所以它得写在用例这一侧。
    REQUIRED_WRITE_TYPES = (
        "WRITE", "INSERT", "ALTER", "CREATE", "DROP", "TRUNCATE", "OPTIMIZE",
        "UNDROP TABLE",
    )

    #: 父类型底下的**子类型**(``ALTER UPDATE`` / ``ALTER DELETE`` / ``ALTER
    #: TABLE`` …)在 ``system.grants`` 里是并列的独立取值,只比字面量会漏掉它们。
    REQUIRED_WRITE_PREFIXES = ("ALTER", "CREATE", "DROP")

    @staticmethod
    def _probe_client(total, writes, hits=""):
        """一个已经连上、``system.grants`` 探测会回这一行结果的 client。

        ``(total, writes, hits)`` 是**照真驱动实测的形状**写的:这条探测是聚合
        查询,真库上 ``query(...).result_rows`` 回来的是「一行三列」的 list of
        tuple,``hits`` 是**一个完整字符串**(实测:``(41, 8, 'ALTER, CREATE, …')``)。

        最容易写错的就是这一层:把 ``result_rows`` 直接写成那个字符串,
        ``result_rows[0]`` 拿到的就成了**第一个字符** —— 而「只读账号」那几条
        用例会因为字符里当然没有 'INSERT' 而**全绿**。假驱动把形状说错一分,
        测试就少测一分。
        """
        return FakeClient([
            FakeResult(["total", "writes", "hits"], [(total, writes, hits)]),
        ])

    async def test_a_read_only_account_is_verified(self, monkeypatch):
        """只读账号的授权行**照样是行**(``total`` 不为零),但写计数为零 → 正面依据。

        三态里 ``True`` 只能建立在正面依据上:这里依据是「查到了这个账号的授权,
        数下来一条写权限都没有」,不是「没查成」。
        """
        client = self._probe_client(3, 0, "")
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(client))
        await adapter.connect()

        probe = await adapter.probe_readonly()

        assert probe.verified is True, "只读账号必须被判成只读"
        assert probe.basis == BASIS_GRANTS

    async def test_the_read_and_write_group_aliases_are_understood(self, monkeypatch):
        """``READ`` / ``WRITE`` 是 CH 的**组别名**,``SELECT`` / ``INSERT`` 是细粒度权限。

        两种表示都要认:只读账号的授权里可能只出现 ``READ``(一个组别名),
        可写账号也可能只出现 ``WRITE``。所以 ``'WRITE'`` 必须在写集合里,
        ``'READ'`` **必须不在** —— 它进写集合的话,每个只读账号都会被报成
        「写得动」,而那句 WARN 的全部价值来自它只在真能写的时候出现。

        这一条断的是**发出去的那串字面量**,不是脚本喂回来的结论:假驱动不执行
        SQL,它没法表达「这行 READ 会不会命中 IN 列表」。字面量在真库上的行为
        (Enum16 能不能这么比、比得对不对)由真库集成用例兜底。
        """
        client = self._probe_client(1, 0, "")
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(client))
        await adapter.connect()

        probe = await adapter.probe_readonly()
        sql = client.queries[0][0]

        assert probe.verified is True
        assert "'WRITE'" in sql, "WRITE 组别名不在写集合里 —— 只授了 WRITE 的账号会被判成只读"
        assert "'READ'" not in sql, "READ 是读组别名,进了写集合就会把只读账号报成写得动"

    async def test_an_account_that_can_write_is_reported_as_writable(self, monkeypatch):
        """写得动 → ``False`` + **依据**。

        依据留在 ``detail`` 里:只说「有 8 条写权限」不够,得说清是哪几种 ——
        运维看到这条 WARN 之后要判断的是「这账号该不该有这些权限」。
        ``hits`` 是**整条取出来的字符串**,不是它的某个字符。
        """
        total, writes, hits = self.REAL_WRITABLE
        client = self._probe_client(total, writes, hits)
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(client))
        await adapter.connect()

        probe = await adapter.probe_readonly()

        assert probe.verified is False, "写得动的账号不能被说成只读"
        assert probe.basis == BASIS_GRANTS
        assert hits in probe.detail, "依据要整条带出,不能只剩一个计数"
        assert str(writes) in probe.detail

    async def test_every_declared_write_access_type_reaches_the_wire(self, monkeypatch):
        """声明「写得动」的类型,就必须真的拿它去查 —— 这是假驱动的盲区。

        假驱动不执行 SQL,脚本说 ``writes=1`` 就是 1:所以「写权限集合被收窄」
        在上面几条用例里**完全看不出来**。收窄的后果只在真库上出现 —— 一个只授了
        ``ALTER`` 的账号会被判成只读(而 ``verified=true`` 是一句替边界背书的话)。
        所以这里把模块常量与真正发出去的 SQL 钉在一起。

        ``toString(access_type) LIKE 'ALTER%'`` 那几个前缀同理:父类型
        (``ALTER`` / ``CREATE`` / ``DROP``)底下还有一批**子类型**
        (``ALTER UPDATE`` / ``ALTER DELETE`` / ``CREATE TABLE`` …),它们在
        ``system.grants`` 里是**独立取值**。只比那 8 个字面量,一个「能改数据但
        没有父类型」的账号会被判成只读 —— 所以这一层也要在线上有。
        """
        client = self._probe_client(1, 0, "")
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(client))
        await adapter.connect()
        await adapter.probe_readonly()

        sql = client.queries[0][0]
        assert set(ch_module._CLICKHOUSE_WRITE_ACCESS_TYPES) == set(self.REQUIRED_WRITE_TYPES), (
            "写权限集合被改变了 —— 要改就得连着这条用例一起改,别让它悄悄变窄"
        )
        assert set(ch_module._CLICKHOUSE_WRITE_ACCESS_PREFIXES) == set(self.REQUIRED_WRITE_PREFIXES)
        missing = [t for t in self.REQUIRED_WRITE_TYPES if f"'{t}'" not in sql]
        assert missing == [], f"这些写权限类型没有进 SQL:{missing}"
        for prefix in self.REQUIRED_WRITE_PREFIXES:
            assert f"LIKE '{prefix}%'" in _squash(sql), (
                f"{prefix} 的子类型没有覆盖 —— 只授了 {prefix} TABLE / "
                f"{prefix} UPDATE 这类细粒度权限的账号会被判成只读"
            )

    async def test_permissions_that_come_through_a_role_are_in_scope(self, monkeypatch):
        """**通过角色拿到 INSERT 的用户不能被判成只读** —— 这是最危险的方向。

        ``system.grants`` 只会列出「授给这个用户」和「授给这个角色」两种行;角色
        继承**不会**自动展开。少一次递归展开,查询就只看得到 ``user_name =
        currentUser()`` 那几行,于是一个通过角色可写的账号在启动日志里是
        ``verified=true``。这一次是**替一道并不存在的边界背书**,而 I1 的全部
        价值就是那道边界真的存在。

        假驱动不执行 SQL,所以这一条钉的是**发出去的那段递归展开本身**(它在
        25.12 上实测可用,形态与设计稿一致)。
        """
        client = self._probe_client(1, 1, "INSERT")
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(client))
        await adapter.connect()
        await adapter.probe_readonly()

        sql = _squash(client.queries[0][0])
        assert (
            "WITH RECURSIVE roles AS ( "
            "SELECT granted_role_name AS r FROM system.role_grants "
            "WHERE user_name = currentUser() UNION ALL "
            "SELECT rg.granted_role_name FROM system.role_grants rg "
            "JOIN roles ON rg.user_name = roles.r )"
        ) in sql, "角色继承没有递归展开 —— 通过角色可写的账号会被判成只读"
        # 展开出来的角色要真的用在那条过滤条件里,否则递归只是白跑一趟
        assert "role_name IN (SELECT r FROM roles)" in sql

    async def test_the_probe_never_sends_a_write_statement(self, monkeypatch):
        """**探测只查权限表,绝不试写**(§4 I1 的实施取舍)。

        设计稿给的另一条路是「尝试一条必然失败的写语句」—— 它「必然失败」的前提
        正是「账号只读」这个待证假设:账号其实可写时,那条语句会**真的写进去**,
        一个探测变成一次生产写入。

        断的是**真正发出去的那串文本**:把引号里的字面量摘掉之后,剩下的部分不许
        再出现任何写/结构动词。``'INSERT'`` 这些词只许以**字面量**的身份出现 ——
        它们是 IN 列表里的比较值,不是语句。
        """
        client = self._probe_client(3, 0, "")
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(client))
        await adapter.connect()
        await adapter.probe_readonly()

        assert len(client.queries) == 1, "探测只发一条查询(3 秒的预算)与一次往返"
        sql = client.queries[0][0]
        assert ";" not in sql, "一次一条语句"
        without_literals = re.sub(r"'[^']*'", "''", sql).upper()
        statement_verbs = (
            "INSERT", "ALTER", "CREATE", "DROP", "TRUNCATE", "OPTIMIZE",
            "UPDATE", "DELETE", "RENAME", "ATTACH", "DETACH", "GRANT", "REVOKE",
        )
        # 按**整词**比,不按子串:这条 SQL 里 ``granted_role_name`` 与
        # ``system.role_grants`` 本身就含 'GRANT' —— 子串比对会把两个列名当成
        # 语句动词(第一版就是这么写的,红在了自己的列名上)。
        for verb in statement_verbs:
            assert not re.search(rf"\b{verb}\b", without_literals), (
                f"{verb} 出现在字面量之外 —— 探测里不许有语句动词"
            )
        # 语句在真驱动上走的是 command(),查询走 query():探测一次都不该碰前者
        assert client.commands == [] and client.command_attempts == 0
        # 引号里那串动词都在:它们是比较值,上面那条 strip 之后看不见它们
        assert "'INSERT'" in sql

    async def test_no_grants_at_all_is_unknown_not_read_only(self, monkeypatch):
        """一行授权都没有 → **不知道**,不是「确认只读」。

        空结果能有的解释不止一种:这个账号的权限可能根本不来自 SQL 授权(那它
        在这张表里就看不见),也可能这条路径没拿到东西。而 ``verified=true`` 是
        一句替边界背书的话 —— 它只能建立在**正面依据**上,不能建立在「没查到」上。
        这一条与 MySQL 的 ``SHOW GRANTS`` 空结果同构。
        """
        client = self._probe_client(0, 0, "")
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(client))
        await adapter.connect()

        probe = await adapter.probe_readonly()

        assert probe.verified is None
        assert probe.basis == BASIS_PROBE_FAILED
        assert probe.detail, "「没查成」也要留下说得出口的理由"

    async def test_an_unexpected_result_shape_is_unknown_not_read_only(self, monkeypatch):
        """形状不对 → **不知道**,同样不往 ``True`` 倒。

        这条探测是聚合查询,必然回「一行三列」。真回来别的形状,说明这条路上拿到
        的不是它该拿到的东西(驱动换了、结果被拍平了)—— 那时候默认「没有写
        权限」就是把一个未知当成一句保证。

        **注意这不吞异常**:查询自己炸掉是另一条路径,那条如实往上抛(见下一条)。
        这里挡的只是「查询明明回来了,但不是我认识的那个形状」。
        """
        for rows in ([], (1, 2, 3)):  # 空结果 / 被拍平成一行三列(少了外层那层)
            client = FakeClient([FakeResult(["total", "writes", "hits"], rows)])
            adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(client))
            await adapter.connect()

            probe = await adapter.probe_readonly()

            assert probe.verified is None, f"形状 {rows!r} 被当成了结论"
            assert probe.basis == BASIS_PROBE_FAILED

    async def test_a_denied_query_is_folded_by_the_orchestrator(self, monkeypatch):
        """查询被拒(权限不足)→ 适配器**如实抛**,折异常是 ``readonly.probe`` 的事。

        适配器在这里吞掉它就等于让「没查成」消失在实现里:调用方看到的都是
        ``None``,但原因(``Not enough privileges`` 还是结果形状不对)只有抛出来
        才留得住 —— 两者的修法不一样。
        """
        client = FakeClient(query_error=RuntimeError(
            "Code: 497. DB::Exception: Not enough privileges",
        ))
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver(client))
        await adapter.connect()

        with pytest.raises(RuntimeError, match="Not enough privileges"):
            await adapter.probe_readonly()

        from trove.services.datasource import readonly
        probe = await readonly.probe(adapter)
        assert probe.verified is None
        assert probe.basis == BASIS_PROBE_FAILED

    async def test_the_probe_before_connect_raises(self, monkeypatch):
        """没连上就不探 —— 与 ``get_schema`` / ``table_profiles`` 同一条判空。"""
        adapter, _ = make_adapter(monkeypatch)
        with pytest.raises(DatasourceError):
            await adapter.probe_readonly()


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

    # ── 只读自检(§4 I1)─────────────────────────────────

    async def test_the_real_account_is_reported_as_writable(self, clickhouse_env):
        """真库上**唯一能证伪**的那一支:这个账号写得动,探测必须说 ``False``。

        为什么非真库不可:

        * 上面那些单测喂的都是脚本,证明不了这条查询在真服务器上**跑得通**。
          这个方言上最容易写错的一处(``Enum16`` 列上套字符串函数)在假驱动上
          完全不报错,只在真库上报 code 43 —— 那会让探测在每次启动时静默退化成
          「不知道」,看起来却一切正常。
        * 另一支(只读账号 → ``True``)在这台容器上**验不了**:这个账号没有
          ``CREATE USER`` 权限(code 497),造不出一个只读账号来,所以那一支只能
          用假驱动覆盖。这里验的是能验的那一半,别把它当成两边都验过了。

        断言 ``False`` 而不是「不是 ``True``」:``None``(没查成)也必须让这条
        用例红 —— 探测在真库上退化成「不知道」,等于这道自检从来没有生效。
        """
        from trove.services.datasource import readonly

        params = clickhouse_env.connection_params
        adapter = ClickHouseAdapter(name="readonly_probe", config={**params})
        await adapter.connect()
        try:
            probe = await adapter.probe_readonly()
            folded = await readonly.probe(adapter)  # 启动路径上真正跑的那一层
        finally:
            await adapter.disconnect()

        assert probe.verified is False, (
            "真库上这个账号有写权限(实测 41 条授权里 8 条是写权限),探测却给出 "
            f"{probe.verified!r}:{probe.detail}"
        )
        assert probe.basis == BASIS_GRANTS
        assert probe.detail, "写得动就得说得出依据"
        assert folded.verified is False, "折了一层之后结论不能变"
        assert folded.basis == BASIS_GRANTS

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
