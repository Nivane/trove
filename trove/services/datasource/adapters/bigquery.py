"""BigQuery database adapter (google-cloud-bigquery, sync → asyncio.to_thread).

本仓**第二个同步驱动**适配器(google-cloud-bigquery 只有同步客户端):所有
I/O 全部 ``asyncio.to_thread`` 包住,事件循环不被阻塞。asyncio 的取消只收回
**等待**,收不回工作线程 —— 所以 ``interrupt()`` 在这里同样是唯一能停下来的
手段,只是打靶的方式由 job 模型天然给出:

* 每条查询就是一个**服务端 job**(``client.query(sql)`` 返回 ``QueryJob``,
  提交的那一刻我们手里就有它);``job.cancel()`` 是官方 API(``jobs.cancel``
  REST)。**不需要**雪花那种"自己注入 requestId 再按它对号入座"的机制 ——
  job 对象本身就是双方都认得的名字,也不存在"查询在飞时 id 还拿不到"。

与雪花适配器的三处结构差异,都是同一个 job 模型带来的:

* **没有 ``_conn_lock``** —— 客户端每次 ``query()`` 起的是独立服务端 job,
  协程之间不共享任何线路/游标状态(客户端只是无状态的 REST 包装)。雪花要
  串行化的是那条**共享连接**,这里没有那个对象可踩。
* **没有重连/重试机制** —— 没有会话状态可过期:凭据由 google-auth 自动
  刷新,单次调用的失败就是那次失败(google-api-core 自己在传输层重试),
  「连接已不可信」这类状态在这个客户端上不存在。读语句失败**不重发**:
  没有需要丢弃的连接,重发只会是第二笔计费 + 第二次副作用风险。
* **终止按 job 对象打**,而不是按语句头 + 注入 id 反查。

列名从 **result schema** 取,不从行里推 —— 这是非 cursor 模型最容易出 bug
的地方:零行结果集里没有任何行可以借出列名,而 schema **依然在**(它来自
job 的查询结果元数据,不是从数据行里推断的)。单测专门钉这条。

Introspection via the **metadata API**(``list_tables`` + 逐表 ``get_table``),
**不是** INFORMATION_SCHEMA:后者是一张**计费表**——每条查它的语句都是一个
查询 job(要扫描),而元数据 API 免费、即时、不产生 job。代价是逐表一次
get,量级是 dataset 的表数(几十上百),可接受。

The driver is imported lazily so the adapter module stays importable
without ``uv sync --extra bigquery``.

**诚实边界(未在真实 GCP 项目验证)**:单测钉的是**我们发出的形状**(假驱动),
服务端语义 —— cancel 是否真让 job 停下、元数据 API 的字段取值 —— 本地无
GCP 项目可验,只有 env-gated 的 ``BIGQUERY_TEST_URL`` 集成测试(CI 不跑)。
**不做**:OAuth 交互式登录(凭据只认 service account 文件或 ADC)、跨
project/location 的联邦查询配置。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from trove.core.types import (
    Capabilities,
    ColumnInfo,
    QueryResult,
    SchemaInfo,
    TableInfo,
    positive_int,
)
from trove.core.errors import DatasourceError, SQLExecutionError
from trove.core.logging import get_logger
from trove.services.datasource.adapters.base import (
    INTERRUPT_TIMEOUT_S,
    DatabaseAdapter,
)

logger = get_logger(__name__)

#: 上一次终止的结果挂在 **asyncio 任务对象**上的属性名(与雪花同构,理由见
#: 那边那段长注释:超时路径上 ``interrupt()`` 已被取消解栈调过一次,而终止
#: 服务是之后才来问的 —— 不留结果,「cancel 被拒」就会被报成 ``kill_sent``)。
_INTERRUPT_OUTCOME_ATTR = "_trove_bigquery_interrupt_outcome"


def _remember_interrupt(task: Any, result: bool) -> bool:
    """记下这次终止的结果并原样返回(供 ``interrupt`` 出口调用)。"""
    if task is not None:
        setattr(task, _INTERRUPT_OUTCOME_ATTR, result)
    return result


def _forget_interrupt(task: Any) -> None:
    """作废本任务的终止结论 —— 新查询一开始就调,免得旧答案替新查询回话。"""
    if task is not None:
        try:
            delattr(task, _INTERRUPT_OUTCOME_ATTR)
        except AttributeError:
            pass


def _get_driver():
    """Import google.cloud.bigquery lazily (raises DatasourceError with a hint when missing)."""
    try:
        from google.cloud import bigquery
        return bigquery
    except ImportError as e:
        raise DatasourceError(
            message="google-cloud-bigquery is not installed — run `uv sync --extra bigquery`",
            datasource="",
        ) from e


def _service_account_credentials(path: str):
    """从 service account JSON 文件建凭据(只在配了 ``service_account_file`` 时走)。

    与 ``_get_driver`` 同一条纪律:导入失败给安装提示,不让 ImportError
    裸着出去。google-auth 是 google-cloud-bigquery 的依赖,装了 extra 就
    一定在 —— 这个分支是"装得很不巧"时的诚实兜底。
    """
    try:
        from google.oauth2 import service_account
    except ImportError as e:
        raise DatasourceError(
            message="google-auth is not installed — run `uv sync --extra bigquery`",
            datasource="",
        ) from e
    return service_account.Credentials.from_service_account_file(path)


class BigQueryAdapter(DatabaseAdapter):
    """BigQuery database adapter via google-cloud-bigquery (sync → to_thread)."""

    label = "BigQuery"
    driver_hint = "`uv sync --extra bigquery`"

    # ``table.num_rows`` 是元数据 API 自带的表级统计(视图没有 → NULL,与
    # 「空表」是两个值,一律 positive_int 归一)。**没有** bytes /
    # last_modified:前者在 BigQuery 上没有与 MySQL 对等的口径(计费口径是
    # 逻辑字节的另算),后者的语义是元数据变更不是数据截止 —— 声明它们就是
    # 替不存在的事实背书(与雪花同一条纪律,§8.4 A)。
    profile_capabilities = frozenset({"row_count"})

    # 终止能力(§7.3):同步驱动 + 服务端 job —— ``job.cancel()`` 是官方
    # API,一条查询一个 job,取消的就是那条(见模块 docstring)。
    supports_interrupt = True

    # DB 侧语句超时(§10):BigQuery **没有会话/连接级的时长机制** —— 唯一
    # 近亲是逐 job 的 ``job_timeout_ms``,但它随查询本身一起发出、只存在于
    # execute 的请求体里,与契约的两个发射通道(连接参数 / 建连后一条语句)
    # 不同源;且它是否真的在服务端独立生效,本地无 GCP 项目可验。∴ 一个字
    # 都不发(照 Doris 收窄 MySQL 的先例):声明 True 而发不出语句,会让
    # 「以为有界其实没有」重新成立。
    supports_statement_timeout = False

    def __init__(self, name: str = "bigquery", config: dict[str, Any] | None = None):
        super().__init__(name, config or {})
        self._client: Any = None
        # 在飞 job:{asyncio 任务 → QueryJob}。**按任务键**,不按适配器键:
        # 终止服务晚一点来问时必须只杀**本任务**那条查询(job 之间本就独立
        # 并发,不像雪花有锁在兜底,这里"只杀自己"更得由结构保证)。
        self._inflight: dict[Any, Any] = {}

    @staticmethod
    def dialect() -> str:
        return "bigquery"

    # ── 连接生命周期 ──────────────────────────────────────

    def _required(self, key: str, what: str) -> str:
        value = str(self.config.get(key, "") or "").strip()
        if not value:
            raise DatasourceError(
                message=f"BigQuery config is missing the {what}",
                datasource=self.name,
            )
        return value

    def _build_client(self, project: str, dataset: str) -> Any:
        """构造并**轻量校验**客户端(在工作线程里跑)。

        校验 = ``get_dataset``:项目/dataset 不存在、或凭据没有访问权,都会
        在这里失败 —— 注册即探测,与其它方言"建连即探测"同一个形状。

        这里**没有**雪花那种标识符形状检查:dataset 名只作为结构化参数进
        元数据 API(``project.dataset`` 字符串),从不拼进 SQL 文本 —— 没有
        可注入的拼接面,就没有需要先验形状的理由。
        """
        bigquery = _get_driver()
        kwargs: dict[str, Any] = {"project": project}
        location = str(self.config.get("location", "") or "").strip()
        if location:
            kwargs["location"] = location
        # credentials 缺席 = 交给客户端走 ADC(应用默认凭据链)。**不传空值**:
        # 空串在这里不是"空凭据",是"截断默认发现链"。
        account_file = str(self.config.get("service_account_file", "") or "").strip()
        if account_file:
            kwargs["credentials"] = _service_account_credentials(account_file)
        client = bigquery.Client(**kwargs)
        client.get_dataset(f"{project}.{dataset}")
        return client

    async def connect(self) -> None:
        if self._client is not None and self._connected:
            return
        # 必填项在**起线程之前**验:缺配置不该以任何形式碰到驱动(与雪花同)。
        project = self._required("project", "project")
        dataset = self._required("dataset", "dataset")
        try:
            self._client = await asyncio.to_thread(self._build_client, project, dataset)
            self._connected = True
            logger.debug(
                "Connected to BigQuery: project=%s dataset=%s location=%s",
                project, dataset, self.config.get("location") or "(default)",
            )
        except DatasourceError:
            raise
        except Exception as e:
            raise DatasourceError(
                message=f"Failed to connect to BigQuery ({project}.{dataset}): {e}",
                datasource=self.name,
            ) from e

    async def disconnect(self) -> None:
        client, self._client = self._client, None
        self._connected = False
        if client is not None:
            try:
                # ``Client.close()`` 关的是本地 HTTP 会话(不发网络请求)——
                # 进线程只为不与工作线程里的调用搅在一起。
                await asyncio.to_thread(client.close)
            except Exception as e:   # 关一个已知要丢的客户端失败,不改变"它已不可用"
                logger.debug("%s dropping client: close failed: %s", self.label, e)

    # ── 终止(§7.3 / I4)──────────────────────────────────

    async def interrupt(self) -> bool:
        """按 job 打 cancel(有界、绝不抛)。

        只杀**本任务**在飞的那条(登记见 ``_inflight``);问过一次就把结果
        记在本任务上,不再发第二次(§10 不重试 kill)。本任务名下没有在飞
        job = 「本就无可取消」→ True(与基类契约一致)。
        """
        task = asyncio.current_task()
        remembered = getattr(task, _INTERRUPT_OUTCOME_ATTR, None) if task else None
        if remembered is not None:
            return remembered
        job = self._inflight.get(task) if task is not None else None
        if job is None:
            return True
        try:
            accepted = await asyncio.wait_for(
                asyncio.to_thread(self._cancel_job, job),
                timeout=INTERRUPT_TIMEOUT_S,
            )
        except Exception as e:
            logger.warning("BigQuery interrupt failed: %s", e)
            return _remember_interrupt(task, False)
        if not accepted:
            # 服务端拒了 cancel(或 job 已结束)—— 与「发不出去」同一条处置:
            # 如实报 False,但**必须留下 WARNING**(证据链的价值全在它不说谎:
            # 静默的话 kill_failed 在日志里查不到任何原因)。
            logger.warning(
                "BigQuery cancel for job %s was not accepted by the server",
                getattr(job, "job_id", "?"),
            )
        return _remember_interrupt(task, bool(accepted))

    @staticmethod
    def _cancel_job(job: Any) -> bool:
        """在工作线程里打 cancel;返回「服务端是否接下了这次终止」。

        ``job.cancel()`` 是官方 API(``jobs.cancel`` REST)。返回值语义没有
        本地可验的凭据(真驱动对"job 已经结束"这类竞态的返回说法不一),所以
        只把**显式 False** 当拒绝、其余按"递交出去了"记 —— 误报方向只能是
        少报一次终止成功,不会是谎报。
        """
        ret = job.cancel()
        return ret is not False

    # ── 执行 ─────────────────────────────────────────────

    async def execute(self, sql: str) -> QueryResult:
        if self._client is None or not self._connected:
            # 从未连接 / 已显式 disconnect:与其它方言一致地快速失败。
            raise SQLExecutionError(message="Not connected to BigQuery", sql=sql)

        start = time.monotonic()
        task = asyncio.current_task()
        try:
            columns, rows = await asyncio.to_thread(self._run_job, sql, task)
        except asyncio.CancelledError:
            # 客户端中止/超时取消:``to_thread`` 收不回那个线程 —— 从另一个
            # 线程打 ``job.cancel()`` 是唯一能让服务端停下来、并让那个线程
            # 尽快从 ``job.result()`` 里解脱的手段。
            await self.interrupt()
            raise
        except Exception as e:
            raise SQLExecutionError(
                message=f"BigQuery execution error: {e}",
                sql=sql,
                db_error=str(e),
            ) from e

        return QueryResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            execution_time_ms=round((time.monotonic() - start) * 1000, 2),
            sql=sql,
            datasource=self.name,
        )

    def _run_job(self, sql: str, task: Any) -> tuple[list[str], list[list[Any]]]:
        """提交 job、等结果、按 schema 取列名(**整段在工作线程里**)。

        登记在飞 job 给 ``interrupt`` 打靶;新查询一开始就作废上一次的终止
        结论(不清的话,同一任务重试第二条查询时会拿到旧答案回话)。
        ``finally`` 里注销 —— 「只杀自己」的另一半:留到下一次,下一次超时
        就会报到一个本任务名下不再在飞的 job 上。
        """
        job = self._client.query(sql)
        if task is not None:
            self._inflight[task] = job
            _forget_interrupt(task)
        try:
            result = job.result()
            # 列名从 **result schema** 取,不从行里推:零行结果集里没有任何
            # 行可以借出列名,而 schema 依然在(job 的查询结果元数据)。
            columns = [str(f.name) for f in result.schema]
            rows = [list(row.values()) for row in result]
            return columns, rows
        finally:
            if task is not None:
                self._inflight.pop(task, None)

    # ── 内省 ─────────────────────────────────────────────

    async def get_schema(self) -> SchemaInfo:
        if self._client is None or not self._connected:
            raise DatasourceError(message="Not connected", datasource=self.name)

        project = self._required("project", "project")
        dataset = self._required("dataset", "dataset")
        try:
            return await asyncio.to_thread(self._introspect, project, dataset)
        except DatasourceError:
            raise
        except Exception as e:
            raise DatasourceError(
                message=f"BigQuery schema introspection failed: {e}",
                datasource=self.name,
            ) from e

    def _introspect(self, project: str, dataset: str) -> SchemaInfo:
        """元数据 API 内省(**在工作线程里跑**),不发任何查询 job。

        ``list_tables`` 拿到表名清单,逐表 ``get_table`` 取完整的表元数据
        (schema / num_rows)。视图与只有元数据的外部表没有 ``num_rows``
        —— 原样带出 None("统计缺失"与"空表"必须是两个值)。
        """
        client = self._client
        dataset_ref = f"{project}.{dataset}"
        tables = []
        for item in client.list_tables(dataset_ref):
            table = client.get_table(f"{dataset_ref}.{item.table_id}")
            tables.append(TableInfo(
                name=str(table.table_id),
                schema=dataset,
                columns=[
                    ColumnInfo(
                        name=str(f.name),
                        type=str(f.field_type),
                        # mode: NULLABLE / REQUIRED / REPEATED。REPEATED 是
                        # 数组字段,归到"可空"一侧(它没有"必须给值"的语义)。
                        nullable=str(f.mode).upper() != "REQUIRED",
                        # BigQuery 的约束**不强制**(字段元数据而非保证,主键
                        # 连"声明"都是新近能力),把声明出来的主键当事实报出去
                        # 会误导生成 —— 一律不报(与雪花同)。
                        primary_key=False,
                    )
                    for f in (table.schema or [])
                ],
                row_count_estimate=positive_int(table.num_rows),
            ))
        # ``list_tables`` 的顺序服务端不保证 —— 按名字排出确定性(测试与
        # diff 都靠它)。
        tables.sort(key=lambda t: t.name)
        return SchemaInfo(tables=tables)

    async def get_capabilities(self) -> Capabilities:
        # 无版本探测:CTE / 窗口 / 事务(GoogleSQL 脚本里的 BEGIN/COMMIT)/
        # JSON 类型(原生 JSON 列型)都是全平台能力。**EXPLAIN 是例外** ——
        # GoogleSQL 没有 ``EXPLAIN`` 语句(查询计划在控制台/INFORMATION_SCHEMA
        # .JOBS 里看),声明 False:``registry.explain`` 会据此拒绝,而不是把
        # 一条必然报错的 SQL 发到线上。
        return Capabilities(
            supports_cte=True,
            supports_window_functions=True,
            supports_transactions=True,
            supports_json_type=True,
            supports_explain=False,
            dialect="bigquery",
        )
