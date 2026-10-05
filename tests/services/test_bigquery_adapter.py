"""BigQuery 适配器单测 —— **假驱动只照真驱动的契约说话**,不照我们希望的样子。

这个方言是仓里第二个同步驱动(google-cloud-bigquery 只有同步客户端),但
与雪花的同步故事有一处结构差别:_job 模型_。``client.query(sql)`` 一提交,
我们手里就有 ``QueryJob`` —— 终止不需要雪花那套「注入 requestId 再按它对号
入座」的机制,job 对象本身就是双方都认得的名字。所以这里钉三件事:

1. ``to_thread`` 包装真的成立(工作线程卡住时事件循环还能推进);
2. **列名从 result schema 取** —— 非 cursor 模型最易出 bug 的地方:零行
   结果集里没有任何行可以借出列名,而 schema 依然在。照行推列名的实现会在
   空结果上返回一个没有列名的结果集,下游全链的形状判断就此失据;
3. 终止按 job 打、只打**本任务在飞**的那条,且「服务端拒了 cancel」不会
   因为「后来没东西可杀」被报成成功(与雪花同一条证据链纪律)。

内省走**元数据 API**(``list_tables`` + 逐表 ``get_table``)而不是
INFORMATION_SCHEMA —— 后者是计费表,查它就是起一个查询 job。单测钉「一次
内省零 job」。

**服务端语义本地验不了**(无真实 GCP 项目):cancel 是否真让 job 停下、
元数据字段取值,只有 env-gated 的 ``BIGQUERY_TEST_URL`` 集成用例(CI 不跑),
适配器 docstring 里明写「未在真实 GCP 验证」。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading

import pytest

from trove.core.errors import DatasourceError, SQLExecutionError
from trove.core.types import BASIS_UNVERIFIABLE, DatasourceConfig
from trove.services.datasource.adapters import bigquery as bq_module
from trove.services.datasource.adapters.bigquery import BigQueryAdapter
from trove.services.datasource.registry import ConnectorRegistry

DEFAULT_CONFIG = {
    "project": "my-project",
    "dataset": "analytics",
}


class FakeSchemaField:
    """真驱动 schema 字段的我们用到的三面:name / field_type / mode。"""

    def __init__(self, name, field_type="STRING", mode="NULLABLE"):
        self.name = name
        self.field_type = field_type
        self.mode = mode


class FakeTable:
    """``get_table`` 的返回:table_id + schema + num_rows(``None`` 合法)。"""

    def __init__(self, table_id, schema=None, num_rows=None):
        self.table_id = table_id
        self.schema = list(schema or [])
        self.num_rows = num_rows


class FakeTableItem:
    """``list_tables`` 的行:只有 table_id。"""

    def __init__(self, table_id):
        self.table_id = table_id


class FakeRow:
    """结果行:适配器只调 ``values()``(按 schema 顺序的值,不带列名)。"""

    def __init__(self, values):
        self._values = list(values)

    def values(self):
        return tuple(self._values)


class FakeResult:
    """RowIterator 的我们用到的两面:schema + 迭代行。

    ``schema`` 是**独立于行**存在的(来自 job 的结果元数据)—— 假体照这个
    形状给,空行列表时 schema 照样在。
    """

    def __init__(self, schema=None, rows=None):
        self.schema = list(schema or [])
        self._rows = list(rows or [])

    def __iter__(self):
        return iter(self._rows)


class FakeJob:
    """QueryJob 的我们用到的三面:result() / cancel() / job_id。"""

    def __init__(self, *, result=None, result_error=None, cancel_result=True,
                 cancel_error=None, blocking=None, cancel_blocking=None,
                 job_id="job-1"):
        self._result = result if result is not None else FakeResult()
        self.result_error = result_error
        self.cancel_result = cancel_result
        self.cancel_error = cancel_error
        self.blocking = blocking              # result() 卡在这里(线程里)
        self.cancel_blocking = cancel_blocking
        self.job_id = job_id
        self.started = threading.Event()
        self.cancels = 0

    def result(self):
        self.started.set()
        if self.blocking is not None:
            self.blocking.wait(timeout=10)
        if self.result_error is not None:
            raise self.result_error
        return self._result

    def cancel(self):
        self.cancels += 1
        if self.cancel_blocking is not None:
            self.cancel_blocking.wait(timeout=10)
        if self.cancel_error is not None:
            raise self.cancel_error
        return self.cancel_result


class FakeClient:
    """``bigquery.Client`` 的假体:查询面 + 元数据面,各自记账。"""

    def __init__(self, *, jobs=None, dataset_error=None, tables=None,
                 table_map=None, list_error=None):
        self._jobs = list(jobs or [])
        self._tables = list(tables or [])
        self._table_map = dict(table_map or {})
        self.dataset_error = dataset_error
        self.list_error = list_error

        self.queries: list[str] = []          # 提交过的 SQL(应当只有 execute 会写)
        self.submitted: list[FakeJob] = []    # query() 返回过的 job
        self.dataset_calls: list[str] = []
        self.list_tables_calls: list[str] = []
        self.get_table_calls: list[str] = []
        self.closed = False

    # ── 查询面 ──────────────────────────────
    def query(self, sql):
        self.queries.append(sql)
        job = self._jobs.pop(0) if self._jobs else FakeJob()
        self.submitted.append(job)
        return job

    # ── 元数据面 ────────────────────────────
    def get_dataset(self, ref):
        self.dataset_calls.append(ref)
        if self.dataset_error is not None:
            raise self.dataset_error
        return object()

    def list_tables(self, ref):
        self.list_tables_calls.append(ref)
        if self.list_error is not None:
            raise self.list_error
        return list(self._tables)

    def get_table(self, ref):
        self.get_table_calls.append(ref)
        return self._table_map[ref]

    def close(self):
        self.closed = True


class FakeDriver:
    """``google.cloud.bigquery`` 模块的假体:``Client(**kwargs)`` 是唯一入口。"""

    def __init__(self, clients=None):
        self._clients = list(clients or [])
        self.client_calls: list[dict] = []    # 每次构造客户端的 kwargs

    def Client(self, **kwargs):
        self.client_calls.append(kwargs)
        return self._clients.pop(0) if self._clients else FakeClient()


def make_adapter(monkeypatch, driver=None, config=None):
    driver = driver or FakeDriver()
    monkeypatch.setattr(bq_module, "_get_driver", lambda: driver)
    adapter = BigQueryAdapter(name="test", config=config or dict(DEFAULT_CONFIG))
    return adapter, driver


# ── 建连 ─────────────────────────────────────────────────


class TestConnect:
    async def test_client_kwargs_travel_together(self, monkeypatch):
        sentinel = object()
        credential_paths: list[str] = []
        monkeypatch.setattr(
            bq_module, "_service_account_credentials",
            lambda path: (credential_paths.append(path), sentinel)[1],
        )
        client = FakeClient()
        adapter, driver = make_adapter(monkeypatch, driver=FakeDriver([client]), config={
            **DEFAULT_CONFIG, "location": "EU",
            "service_account_file": "/keys/gcp.json",
        })
        await adapter.connect()

        assert adapter.is_connected
        kwargs = driver.client_calls[0]
        assert kwargs["project"] == "my-project"
        assert kwargs["location"] == "EU"
        assert kwargs["credentials"] is sentinel
        assert credential_paths == ["/keys/gcp.json"]
        # 建连探测就是 get_dataset(项目/dataset 不存在或没权限在这里失败)
        assert client.dataset_calls == ["my-project.analytics"]

    async def test_unset_optionals_stay_out_of_the_client_body(self, monkeypatch):
        """没配 location / service_account_file 时不带键 —— 交给客户端走 ADC。

        空串在这里不是"空凭据",是**截断默认发现链**:宁可让 ADC 自己失败,
        也不拿一个空值替它做决定。
        """
        adapter, driver = make_adapter(monkeypatch)
        await adapter.connect()

        assert driver.client_calls[0] == {"project": "my-project"}

    async def test_the_dataset_probe_runs_at_connect(self, monkeypatch):
        client = FakeClient()
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver([client]))
        await adapter.connect()
        assert client.dataset_calls == ["my-project.analytics"]

    async def test_a_missing_project_is_a_config_error(self, monkeypatch):
        adapter, driver = make_adapter(
            monkeypatch,
            config={k: v for k, v in DEFAULT_CONFIG.items() if k != "project"},
        )
        with pytest.raises(DatasourceError, match="project"):
            await adapter.connect()
        assert driver.client_calls == [], "缺配置不该以任何形式碰到驱动"

    async def test_a_missing_dataset_is_a_config_error(self, monkeypatch):
        adapter, driver = make_adapter(
            monkeypatch,
            config={k: v for k, v in DEFAULT_CONFIG.items() if k != "dataset"},
        )
        with pytest.raises(DatasourceError, match="dataset"):
            await adapter.connect()
        assert driver.client_calls == []

    async def test_a_failing_probe_wraps_datasource_error(self, monkeypatch):
        client = FakeClient(dataset_error=RuntimeError(
            "Not found: Dataset my-project:analytics was not found in location US",
        ))
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver([client]))
        with pytest.raises(DatasourceError, match="was not found"):
            await adapter.connect()
        assert not adapter.is_connected

    async def test_the_missing_driver_carries_the_install_hint(self, monkeypatch):
        def _missing():
            raise DatasourceError(
                message="google-cloud-bigquery is not installed — run "
                        "`uv sync --extra bigquery`",
                datasource="",
            )

        monkeypatch.setattr(bq_module, "_get_driver", _missing)
        adapter = BigQueryAdapter(name="test", config=dict(DEFAULT_CONFIG))
        with pytest.raises(DatasourceError) as exc_info:
            await adapter.connect()
        assert "uv sync --extra bigquery" in str(exc_info.value)

    def test_the_real_get_driver_reports_the_hint_when_import_fails(self, monkeypatch):
        """真 ``_get_driver`` 自己的提示(不是测试替身演的)。

        ``sys.modules`` 里放 None 是让 ``from google.cloud import bigquery``
        必然 ImportError 的标准做法 —— 不管这台机器装没装驱动,这条都成立。
        """
        monkeypatch.setitem(sys.modules, "google.cloud", None)
        monkeypatch.setitem(sys.modules, "google.cloud.bigquery", None)
        with pytest.raises(DatasourceError) as exc_info:
            bq_module._get_driver()
        assert "uv sync --extra bigquery" in str(exc_info.value)

    async def test_disconnect_closes_the_client(self, monkeypatch):
        client = FakeClient()
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver([client]))
        await adapter.connect()
        await adapter.disconnect()

        assert not adapter.is_connected
        assert client.closed

    async def test_a_close_that_fails_does_not_raise(self, monkeypatch):
        """关一个已知要丢的客户端失败,不改变「它已不可用」这个事实。"""

        class _BadClose(FakeClient):
            def close(self):
                raise RuntimeError("already closed")

        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver([_BadClose()]))
        await adapter.connect()
        await adapter.disconnect()   # 不抛
        assert not adapter.is_connected


# ── 执行(含 to_thread 包装 + 列名从 schema 取)─────────────


class TestExecute:
    async def test_execute_returns_query_result(self, monkeypatch):
        job = FakeJob(result=FakeResult(
            schema=[FakeSchemaField("ID", "INTEGER"), FakeSchemaField("NAME")],
            rows=[FakeRow([1, "a"]), FakeRow([2, "b"])],
        ))
        client = FakeClient(jobs=[job])
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver([client]))
        await adapter.connect()

        result = await adapter.execute("SELECT id, name FROM t")
        assert result.columns == ["ID", "NAME"]
        assert result.rows == [[1, "a"], [2, "b"]]
        assert result.row_count == 2
        assert result.datasource == "test"
        assert client.queries == ["SELECT id, name FROM t"]

    async def test_an_empty_result_set_still_carries_the_columns(self, monkeypatch):
        """零行结果集:没有行可以借出列名,而 schema **依然在**(job 的结果元数据)。

        列名从行里推的实现在这里会返回 ``columns == []`` —— 下游 select /
        validate / masking / 输出拿一个没有列名的结果集,形状判断全部失据。
        这条就是这个方言最容易出 bug 的那一处。
        """
        job = FakeJob(result=FakeResult(
            schema=[
                FakeSchemaField("loan_id", "INTEGER"),
                FakeSchemaField("amount", "NUMERIC"),
            ],
            rows=[],
        ))
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver([FakeClient(jobs=[job])]))
        await adapter.connect()

        result = await adapter.execute("SELECT loan_id, amount FROM loan WHERE 1 = 0")
        assert result.columns == ["loan_id", "amount"]
        assert result.rows == []
        assert result.row_count == 0

    async def test_the_event_loop_advances_while_the_job_waits(self, monkeypatch):
        """同步驱动被包在线程里 —— 工作线程卡住时事件循环还能跑别的协程。

        去掉 ``to_thread`` 的回归表现:别的协程全部停摆(这条用例在超时前
        永远数不到 tick)。这正是"取消只是放弃等待"之下必须成立的前提 ——
        等不到这一步,连取消的机会都不会有。
        """
        release = threading.Event()
        job = FakeJob(
            blocking=release,
            result=FakeResult(schema=[FakeSchemaField("X")], rows=[FakeRow([1])]),
        )
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver([FakeClient(jobs=[job])]))
        await adapter.connect()

        async def _while_blocked() -> int:
            task = asyncio.create_task(adapter.execute("SELECT 1"))
            while not job.started.is_set():
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
        job = FakeJob(result_error=RuntimeError("Syntax error: Unexpected keyword FROM"))
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver([FakeClient(jobs=[job])]))
        await adapter.connect()

        with pytest.raises(SQLExecutionError) as exc_info:
            await adapter.execute("SELECT * FORM t")
        assert "Syntax error" in str(exc_info.value)

    async def test_a_failure_is_not_retried(self, monkeypatch):
        """没有会话状态可过期 → 没有重连/重发:失败就是那次失败。

        重发只会是第二笔计费(+ 写语句的第二次副作用风险);google-api-core
        自己在传输层重试,那是它的事。
        """
        job = FakeJob(result_error=RuntimeError("boom"))
        client = FakeClient(jobs=[job])
        adapter, driver = make_adapter(monkeypatch, driver=FakeDriver([client]))
        await adapter.connect()

        with pytest.raises(SQLExecutionError):
            await adapter.execute("SELECT 1")

        assert len(client.queries) == 1, "失败不许重发"
        assert len(driver.client_calls) == 1, "没有重连"

    async def test_the_inflight_registration_is_cleared_after_the_run(self, monkeypatch):
        """跑完即注销 —— 留到下一次,下一次超时会报到一个不再在飞的 job 上。"""
        job = FakeJob(result=FakeResult(schema=[FakeSchemaField("X")], rows=[FakeRow([1])]))
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver([FakeClient(jobs=[job])]))
        await adapter.connect()

        await adapter.execute("SELECT 1")
        assert adapter._inflight == {}
        assert await adapter.interrupt() is True   # 跑完之后:无可取消


# ── 终止(§7.3 / I4)────────────────────────────────────


class TestTermination:
    async def test_nothing_in_flight_reports_true_and_sends_nothing(self, monkeypatch):
        """「本就无可取消」是 ``True`` —— 与基类契约一致(不是"没发出去")。"""
        client = FakeClient()
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver([client]))
        await adapter.connect()

        assert await adapter.interrupt() is True
        assert client.submitted == []

    async def test_a_cancelled_query_is_cancelled_by_its_own_job(self, monkeypatch):
        """取消栈上那次 ``interrupt()`` 必须打中**本任务正在飞**的那条查询。

        job 模型下靶子就是 ``client.query`` 返回的那个对象 —— 不需要雪花那套
        id 反查(登记见 ``_inflight``,按任务键)。
        """
        release = threading.Event()
        job = FakeJob(blocking=release)
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver([FakeClient(jobs=[job])]))
        await adapter.connect()

        task = asyncio.create_task(adapter.execute("SELECT sleep(30)"))
        while not job.started.is_set():
            await asyncio.sleep(0.01)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()

        assert job.cancels == 1, "取消栈上的 interrupt 必须打中本任务在飞的那条"

    async def test_a_rejected_cancel_is_not_reported_as_sent_when_asked_later(
        self, monkeypatch, caplog,
    ):
        """**「服务端拒了 cancel」不能因为「后来没东西可杀」变成成功。**

        超时路径上 ``interrupt()`` 被取消解栈调过一次(结果没人接),而
        ``QueryTerminator`` 是**之后**才来问的 —— 那时 ``_inflight`` 已在
        ``finally`` 里注销。不记结果,第二次只能回一个干净利落的 ``True``,
        折成证据里的 ``kill_sent``:查询还在跑,答案写着"已发出终止指令"。

        走 ``asyncio.wait_for`` 而不是 ``create_task`` 是刻意的 —— 3.12 里
        ``wait_for`` 用 ``async with timeout`` 在**同一任务**里跑,与节点里的
        调用形态逐字一致,连"谁来问"这个任务身份都不差。
        """
        release = threading.Event()
        job = FakeJob(blocking=release, cancel_result=False)
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver([FakeClient(jobs=[job])]))
        await adapter.connect()

        with caplog.at_level(logging.WARNING):
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    adapter.execute("SELECT sleep(30)"), timeout=0.05,
                )
        release.set()

        assert await adapter.interrupt() is False, "把被拒的终止报成了成功"
        assert job.cancels == 1, "同一条查询只终止一次(§10 不重试 kill)"
        assert any("not accepted" in r.message for r in caplog.records), (
            "被拒的 cancel 必须留下 WARNING —— 静默失败在日志里查不到原因"
        )

    async def test_a_cancel_that_raises_is_a_warning_not_an_exception(
        self, monkeypatch, caplog,
    ):
        """cancel 发不出去:取消照旧解栈(仍抛 CancelledError),但人得看得见。"""
        release = threading.Event()
        job = FakeJob(blocking=release, cancel_error=RuntimeError("cancel denied"))
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver([FakeClient(jobs=[job])]))
        await adapter.connect()

        with caplog.at_level(logging.WARNING):
            task = asyncio.create_task(adapter.execute("SELECT sleep(30)"))
            while not job.started.is_set():
                await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        release.set()

        assert any("cancel denied" in r.message for r in caplog.records)
        assert job.cancels == 1, "同一条查询只终止一次(§10)"
        # 结论记在**发起这条查询的任务**上:之后同一个任务来问才会拿到它。
        # 换一个任务来问 → 本任务名下没有在飞的查询 → True:这正是"只杀自己"
        # 的另一半,不是"这次 cancel 成功了"。
        assert getattr(task, bq_module._INTERRUPT_OUTCOME_ATTR) is False
        assert await adapter.interrupt() is True

    async def test_a_cancel_that_hangs_is_bounded_by_the_interrupt_timeout(
        self, monkeypatch,
    ):
        """cancel 卡住 → ``INTERRUPT_TIMEOUT_S`` 后报 False,**绝不悬挂**终止路径。"""
        monkeypatch.setattr(bq_module, "INTERRUPT_TIMEOUT_S", 0.05)

        class _SlowCancelJob(FakeJob):
            def cancel(self):
                self.cancels += 1
                threading.Event().wait(timeout=0.5)   # 比界长,但不拖整轮
                return True

        job = _SlowCancelJob()
        adapter, _ = make_adapter(monkeypatch)
        adapter._inflight[asyncio.current_task()] = job

        assert await adapter.interrupt() is False

    async def test_a_new_query_forgets_the_previous_outcome(self, monkeypatch):
        """上一次终止的结论属于上一条查询 —— 新查询开始必须作废它。

        不清理的话,同一任务里重试第二条查询时 ``interrupt()`` 会拿旧结论
        回话:一条正在飞的查询会因为"上一条 cancel 被拒"被判死刑。
        """
        release = threading.Event()
        first = FakeJob(blocking=release, cancel_result=False)
        second = FakeJob(result=FakeResult(
            schema=[FakeSchemaField("X")], rows=[FakeRow([2])],
        ))
        adapter, _ = make_adapter(
            monkeypatch, driver=FakeDriver([FakeClient(jobs=[first, second])]),
        )
        await adapter.connect()

        # 第一条:超时取消 → cancel 被拒 → False 被记在本任务上
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(adapter.execute("SELECT sleep(30)"), timeout=0.05)
        assert await adapter.interrupt() is False
        release.set()

        # 第二条:新查询一开始就作废旧结论 → 跑完后再问是"无可取消"的 True
        assert (await adapter.execute("SELECT 2")).rows == [[2]]
        assert await adapter.interrupt() is True


# ── 内省(元数据 API,零查询 job)──────────────────────────


class TestGetSchema:
    def _client(self):
        return FakeClient(
            tables=[
                FakeTableItem("sales"),
                FakeTableItem("dim_date"),
                FakeTableItem("v_meta"),
                FakeTableItem("zeroed"),
            ],
            table_map={
                "my-project.analytics.sales": FakeTable("sales", schema=[
                    FakeSchemaField("id", "INTEGER", "REQUIRED"),
                    FakeSchemaField("amount", "NUMERIC", "NULLABLE"),
                    FakeSchemaField("tags", "STRING", "REPEATED"),
                ], num_rows=42),
                "my-project.analytics.dim_date": FakeTable("dim_date", schema=[
                    FakeSchemaField("d", "DATE", "REQUIRED"),
                ], num_rows=7),
                "my-project.analytics.v_meta": FakeTable(
                    "v_meta", schema=[FakeSchemaField("k")], num_rows=None,   # 视图:无统计
                ),
                "my-project.analytics.zeroed": FakeTable("zeroed", schema=[], num_rows=0),
            },
        )

    async def test_reads_the_metadata_api_and_groups_columns_by_table(self, monkeypatch):
        client = self._client()
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver([client]))
        await adapter.connect()

        schema = await adapter.get_schema()

        # list_tables 的顺序服务端不保证 → 输出按名字确定
        assert [t.name for t in schema.tables] == ["dim_date", "sales", "v_meta", "zeroed"]
        by_name = {t.name: t for t in schema.tables}
        sales = by_name["sales"]
        assert sales.schema == "analytics"
        assert sales.row_count_estimate == 42
        assert [c.name for c in sales.columns] == ["id", "amount", "tags"]
        cols = {c.name: c for c in sales.columns}
        assert cols["id"].type == "INTEGER"
        assert cols["id"].nullable is False          # REQUIRED
        assert cols["amount"].nullable is True
        assert cols["tags"].nullable is True         # REPEATED 归"可空"一侧
        # BigQuery 的约束**不强制** —— 声明出来的主键不是保证,一律不报
        assert all(c.primary_key is False for c in sales.columns)
        # 视图没有 num_rows(统计缺失)与空表(0)都归一为 None 以外的语义:
        # positive_int 把"0 不是依据"折成 None,"没有统计"也是 None
        assert by_name["v_meta"].row_count_estimate is None
        assert by_name["zeroed"].row_count_estimate is None
        assert by_name["zeroed"].columns == []

        # 内省只走元数据 API:list_tables 一次 + 逐表 get_table(按清单顺序)
        assert client.list_tables_calls == ["my-project.analytics"]
        assert client.get_table_calls == [
            "my-project.analytics.sales",
            "my-project.analytics.dim_date",
            "my-project.analytics.v_meta",
            "my-project.analytics.zeroed",
        ]

    async def test_no_query_job_is_launched_for_introspection(self, monkeypatch):
        """INFORMATION_SCHEMA 是**计费表** —— 内省一个 SQL 都不发。"""
        client = self._client()
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver([client]))
        await adapter.connect()

        await adapter.get_schema()

        assert client.queries == []

    async def test_introspection_failure_wraps_datasource_error(self, monkeypatch):
        client = FakeClient(list_error=RuntimeError("permission denied on dataset"))
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver([client]))
        await adapter.connect()

        with pytest.raises(DatasourceError, match="permission denied"):
            await adapter.get_schema()

    async def test_get_schema_before_connect_raises(self, monkeypatch):
        adapter, _ = make_adapter(monkeypatch)
        with pytest.raises(DatasourceError, match="Not connected"):
            await adapter.get_schema()


# ── 声明 ─────────────────────────────────────────────────


class TestDeclarations:
    async def test_capabilities(self, monkeypatch):
        adapter, _ = make_adapter(monkeypatch)
        caps = await adapter.get_capabilities()
        assert caps.dialect == "bigquery"
        assert caps.supports_cte is True
        assert caps.supports_window_functions is True
        assert caps.supports_transactions is True
        assert caps.supports_json_type is True   # 原生 JSON 列型
        # GoogleSQL 没有 EXPLAIN 语句 —— 声明 False,registry.explain 据此拒绝
        assert caps.supports_explain is False

    async def test_profile_capabilities_declare_only_row_count(self, monkeypatch):
        """bytes / last_modified 在 BigQuery 上没有可验证的对等口径 —— 不声明
        就是不替它们背书(与雪花同一条纪律)。"""
        adapter, _ = make_adapter(monkeypatch)
        assert adapter.profile_capabilities == frozenset({"row_count"})

    async def test_the_readonly_probe_is_honestly_unverifiable(self, monkeypatch):
        """这一版**不写**专属只读探测:基类默认如实报「未验证」。

        写一个"查 IAM 权限"的探测需要它在真 GCP 项目上验证过 —— 没验过的
        探测比没有探测更糟:``verified=True`` 是一句替边界背书的话。
        """
        client = FakeClient()
        adapter, _ = make_adapter(monkeypatch, driver=FakeDriver([client]))
        await adapter.connect()

        probe = await adapter.probe_readonly()
        assert probe.verified is None
        assert probe.basis == BASIS_UNVERIFIABLE
        assert client.queries == []   # 探测没有偷偷发查询

    async def test_the_statement_timeout_is_honestly_undeclared(self, monkeypatch):
        """没有会话级时长机制 → 一个字都不发(照 Doris 收窄 MySQL 的先例)。

        声明能力却发不出语句,会让「以为有界其实没有」重新成立;契约矩阵由
        ``test_adapter_statement_timeout_contract.py`` 钉住,这里钉三个出口都
        是"没有"。
        """
        adapter, _ = make_adapter(monkeypatch)
        assert adapter.supports_statement_timeout is False
        assert adapter.statement_timeout_ms() is None
        assert adapter.statement_timeout_connect_kwargs() == {}
        assert await adapter.apply_statement_timeout() is False

    def test_dialect_static(self):
        assert BigQueryAdapter.dialect() == "bigquery"


class TestExplainRefusal:
    """能力位够到执行面:``registry.explain`` 在 ``supports_explain=False`` 上
    拒绝 —— 「不支持」与「执行失败」对调用方是两件事。"""

    async def test_registry_refuses_explain_for_bigquery(self, monkeypatch):
        client = FakeClient()
        monkeypatch.setattr(bq_module, "_get_driver", lambda: FakeDriver([client]))
        registry = ConnectorRegistry()
        await registry.register(DatasourceConfig(
            name="bq", type="bigquery",
            connection_params={"project": "my-project", "dataset": "analytics"},
        ))

        with pytest.raises(DatasourceError, match="EXPLAIN is not supported"):
            await registry.explain("SELECT 1", "bq")

        assert client.queries == [], "拒绝必须发生在发 SQL 之前"
        await registry.close_all()


# ── 集成(BIGQUERY_TEST_URL,未设则跳过;CI 不跑)────────────


@pytest.mark.integration
class TestBigQueryIntegration:
    """**本地无 GCP 项目,服务端语义只能在真环境验** —— 设了
    ``BIGQUERY_TEST_URL`` 才会跑。这里只做最小闭环(连上 → 查一条 → 内省一次),
    验的是「发出去的形状在真服务端被接受」;终止与元数据字段取值的服务端语义
    由人工在真环境按适配器 docstring 里那张单子验。
    """

    @pytest.fixture
    async def bigquery_adapter(self):
        url = os.environ.get("BIGQUERY_TEST_URL")
        if not url:
            pytest.skip("BIGQUERY_TEST_URL not set")
        from trove.services.datasource.urls import parse_datasource_url

        cfg = parse_datasource_url(url)
        adapter = BigQueryAdapter(
            name="integration", config=dict(cfg.connection_params),
        )
        await adapter.connect()
        try:
            yield adapter
        finally:
            await adapter.disconnect()

    async def test_full_lifecycle(self, bigquery_adapter):
        result = await bigquery_adapter.execute("SELECT 1 AS one")
        assert result.columns == ["one"]
        assert result.rows == [[1]]

        schema = await bigquery_adapter.get_schema()
        assert schema.tables, "数据集里应当有表;空清单说明 project/dataset 没对上"
