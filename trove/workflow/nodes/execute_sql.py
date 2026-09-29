"""ExecuteSQL node — runs generated SQL against the datasource.

Cancellation is handled by asyncio task cancellation (CancelledError
propagates through the graph); no explicit cancellation-event checks.

Node shape: `async def execute_sql(state: WorkflowState) -> dict`
returns a partial state update.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from trove.core.i18n import L
from trove.core.logging import get_logger
from trove.core.metrics import (
    record_authz_deny,
    record_authz_table_warn,
    record_sql_budget_decision,
    record_sql_degraded,
    record_sql_kill,
)
from trove.services.authz.enforcer import Authorizer, referenced_tables
from trove.services.authz.policy import principal_from_wire
from trove.services.datasource.registry import ConnectorRegistry
from trove.services.limits import get_result_limits
from trove.services.errors import is_transient, tag_error
from trove.services.sql.budget import (
    BudgetDecision,
    BudgetService,
    CostEstimate,
    ExecutionBudget,
    execution_evidence,
    force_limit,
)
from trove.services.semantic_layer.compiler import (
    build_contract,
    compiled_sql_matches,
    skeleton_preserved,
)
from trove.services.semantic_layer.contract import contract_from_wire
from trove.llm.observability import record_span
from trove.workflow.state import WorkflowState, budget_exhausted

logger = get_logger(__name__)

# 编译照抄校验失败的错误前缀:analyze_error 据此走确定性短路径(不烧 LLM)。
COMPILE_DRIFT_TAG = "[ERR:COMPILE_DRIFT]"


def make_execute_sql(
    connectors: ConnectorRegistry | None = None,
    timeout_ms: int = 30000,
    max_retries: int = 10,
    lineage=None,
    budget: BudgetService | None = None,
    authorizer: Authorizer | None = None,
    profiles: Any = None,
    terminator: Any = None,
) -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
    """Build the execute_sql node bound to a connector registry.

    Args:
        connectors: Registry used to run SQL (None → error update).
        timeout_ms: Query timeout in milliseconds.
        max_retries: Shared correction budget — execution failures feed
            back to gen_sql for regeneration while retry_count < max_retries;
            once exhausted, failures degrade gracefully via state.error.
        lineage: Optional LineageService — successful queries are recorded
            as downstream (consumer) lineage facts for the datasource.
        budget: 执行画像的成本轨(设计 §5.1 / §5.2)。执行前估算代价 → 三档
            处置(allow / degrade / reject),估算不可得时**加 LIMIT 降级执行**
            并把 ``ExecutionEvidence`` 写进 state。``None`` = **未装配强制
            点**,不判也不算 —— 嵌入场景(纯 stdio 本地库)显式关掉它,行为
            与改造前逐字一致。注意这与「判过了、放行」不是一回事,本节点不会
            假装自己判过。
        authorizer: 执行前授权门(设计 §5.3 / G2)。``None`` = **未装配强制点**,
            不判——生产图恒装配一个(见 ``graphs.py``)。注意这与「判过了、
            放行」不是一回事,本节点不会假装自己判过。
        profiles: 画像服务(设计 §8.4 / I5),只用到 ``freshness``:成功路径上
            问一次「这份数据截止到什么时候」,连同口径写进执行证据。``None``
            = 没查过(证据里 ``as_of_basis=""``),不是「查过但不知道」
            (那是 ``"unknown"``)。只有 ``budget`` 装了才有证据块可落,与
            ``graphs._build_profile`` 同开关同实例(共用一份 TTL 缓存)。
        terminator: 终止服务(设计 §7.3 / I4 / §10)。**只在放弃那一次**问它
            —— 重试预算还没用完就终止,等于把下一次尝试也杀掉。它做的事是
            「报告那条查询有没有被主动终止」而不是「再发一次 kill」:超时路径上
            适配器的 ``execute`` 已经发过了(``asyncio.wait_for`` 取消的正是
            它),而 §10 明说不重试 kill。``None`` = **没试过**(证据里
            ``kill=""``),不是「不支持」也不是「失败」—— 那两种都是查过之后
            的结论。

    Returns:
        Async node function taking WorkflowState and returning a partial update.
    """

    async def execute_sql(state: WorkflowState) -> dict[str, Any]:
        # Upstream node failed — pass through without running
        if state.error:
            return {}

        if not state.sql:
            return {"error": "No SQL to execute — SQL generation did not produce a query."}

        if connectors is None:
            return {"error": "No datasource registry available."}

        # AST 级只读防火墙兜底(最终执行路径的最后一道防线):gen_sql 的
        # validate/explain 工具已校验过,但绕开 gen_sql 直达执行的 SQL
        # (API 直执行 / job payload / MCP 工具等)必须在这里被拦下——写语句
        # /data-modifying CTE/元数据表侦察/危险函数一律硬拒,不回生成炉
        # (安全违规不是可修正的生成缺陷)。解析失败 fail-open(方言盲区
        # 不等于权限违规;真实边界在数据库侧只读角色)。
        try:
            from trove.services.sql.guard import check_readonly

            ok, reasons = check_readonly(
                state.sql, state.dialect or "", fail_on_parse_error=False,
            )
            if not ok:
                logger.warning(
                    "SQL guard rejected %r: %s",
                    state.question[:80], "; ".join(reasons),
                )
                return {
                    "error": (
                        "[ERR:SQL_GUARD] The SQL was blocked by the read-only "
                        f"guard: {'; '.join(reasons)}"
                    ),
                    "columns": [],
                    "rows": [],
                    "row_count": -1,
                }
        except Exception as e:
            logger.warning("SQL guard skipped (fail-open): %s", e)

        # 编译照抄校验(确定性 diff,执行前):compiled 通道的 SQL 必须等价
        # 复现编译器拼出的权威 SQL——偏离(改聚合/加别名/调 join 等)直接
        # 打回 gen_sql 重生成,而不是把被 LLM 改坏的 SQL 拿去执行。该偏离
        # 进入统一修正轮(analyze_error 确定性短路径 + versions 回归链)。
        # 分级逃生梯:partial(软 MISS 骨架)走骨架保真校验——join/过滤/分组
        # 必须保留,投影允许 LLM 补缺。
        if state.compiled and state.compiled_sql:
            # 权威结构从**契约**取(Phase A0):同一份结构不必再经"编译期
            # 序列化成字符串 → 这里反推回来"的往返。A1 把结构在编译期抽好
            # (join 边/过滤/分组/形状签名),这里只解析生成 SQL 一条。
            # 契约缺席(旧 checkpoint / wire 形状异常)→ 退回 compiled_sql
            # 现抽一份 —— 与改造前逐字一致,不会因为读不到契约就静默放松。
            contract = contract_from_wire(state.contract)
            if contract is None:
                contract = build_contract(state.compiled_sql, state.dialect)
            if state.compile_partial:
                ok, why = skeleton_preserved(contract, state.sql, state.dialect)
            else:
                ok, why = compiled_sql_matches(contract, state.sql, state.dialect)
            if not ok:
                logger.info("compile drift for %r: %s", state.question[:80], why)
                return _compile_drift_failure(state, max_retries)

        # 执行前授权门(设计 §5.3 / G2)—— **执行前的最后一米**。放在这里而不是
        # 更靠前,是因为上面几道守卫都只在「这一轮反正要跑」的假设下才有意义:
        # 授权是唯一一道会改变「要不要执行」的判定,它之后紧接着就是落库。
        #
        # 无论 SQL 从哪来(编译器 / 快径 / API 直执行 / job payload)都过这里:
        # 存量的授权是路由的装饰器(``Depends(require_datasource)``),语义是
        # 「这个**端点**需要授权」,于是任何不经路由的入口都绕开了它。
        #
        # 拒绝**不可修正** —— 与只读门同理,不喂回 gen_sql 重生成:让模型重写
        # 十遍也改不掉「这个用户没有这张表的权限」,只会烧掉共享修正预算。
        authz_extra: dict[str, Any] = {}
        if authorizer is not None:
            decision = authorizer.check(
                principal_from_wire(state.principal),
                datasource=state.datasource or "",
                sql=state.sql,
                default=connectors.default_name,
                dialect=state.dialect or "",
            )
            authz_extra["authz_decision"] = {
                "allowed": decision.allowed,
                "reason": decision.reason,
                "narrowed_tables": list(decision.narrowed_tables),
                "datasource": state.datasource or connectors.default_name or "",
            }
            if not decision.allowed:
                logger.warning(
                    "authz denied %r: %s (datasource=%r)",
                    state.question[:80], decision.reason,
                    state.datasource or connectors.default_name,
                )
                # 拒绝计数记在**这里**,不记在 ``Authorizer`` 里:同一个
                # ``check`` 也服务于「只判定不执行」的调用,而那些没有落库。
                # 记在服务层会把它们算进拒绝率,运维拿这个数报警会打到空处
                # (与成本轨「节点记才等于这条查询落库了」同一条纪律)。
                record_authz_deny(decision.reason)
                return {
                    "error": _authz_message(decision),
                    **authz_extra,
                }
            if decision.narrowed_tables:
                # warn 期(§8.2)的主要产物:放行了,但记下「哪些表会被拒」。
                # 三条出口各答一个问题,缺一条这个观察期就白跑:
                #   日志 → 这一次的现场;计数器 → 量(切 enforce 会打挂多少);
                #   审计行(agent/session.py 的 authz.table_warn)→ 名字(哪些表)。
                # 名字**不**进计数器:表名基数无限(记在这里同 record_authz_deny
                # 与拒绝计数分家的理由 —— 放行不是拒绝,并进去会污染告警率)。
                logger.warning(
                    "authz table warning for %r: %s (mode=warn)",
                    state.question[:80], ", ".join(decision.narrowed_tables),
                )
                record_authz_table_warn(
                    state.datasource or getattr(connectors, "default_name", "") or ""
                )

        # 指标用的数据源名:与血缘、终止、证据同一个解析(state.datasource → 默认源)。
        # 三处各解析一次,就会出现「指标记的是默认源、线索记的是另一个」这种对不上
        # 的账 —— 而这类账的排查成本随部署里的数据源数量上升。
        #
        # 用 getattr 而不是直接取属性:``connectors`` 是鸭子类型(节点只需要
        # ``execute``),手上没有 ``default_name`` 的替身不该因为「记一次指标」而
        # 炸掉整条执行路径 —— 指标不能拖垮请求路径这条纪律,在这里落到「连取值
        # 都不许抛」。
        metric_datasource = state.datasource or getattr(connectors, "default_name", "") or ""

        # 成本轨(设计 §5.1 / §5.2)—— 权限轨之后、落库之前。
        #
        # 排序有讲究:授权判定放在**前面**,因为被拒的查询不该再去 EXPLAIN。
        # EXPLAIN 本身要落库(它就是一条查询),在一片 deny 的部署里那是一整条
        # 噪声链路;而反过来不成立 —— 成本判定不影响「要不要执行」的资格。
        #
        # 与权限轨**方向相反**且刻意如此(§8.1):权限轨有下层兜底(DB 只读角色),
        # 误拒会阻断正常业务 → fail-open;成本轨没有下层兜底(只读角色不阻止一条
        # 扫 10TB 的 SELECT),误放行的代价是打爆生产库 → fail-closed。
        executed_sql = state.sql
        budget_extra: dict[str, Any] = {}
        if budget is not None:
            est, decision, effective_budget = await _judge_budget(budget, state)
            # 成本判定落账(设计 §9.2 / R1)。记录点**在节点、不在 BudgetService**:
            # 同一套 ``estimate``/``decide`` 在 ``describe_cost``(生成前问价)里也
            # 会跑一遍,在服务层记会把「问价的」和「真跑了的」混成一个数 —— 问价
            # 不碰数据库,真跑了的那条可能打爆生产库。节点记才等于「这条查询落库了」。
            #
            # 判定一经作出就记(在下面几个 return 之前):reject 也是判定结果,
            # 漏掉它,这个分布就答不出「被拒的那些是哪一档判的」。
            record_sql_budget_decision(metric_datasource, est.source, decision.verdict)
            if decision.verdict == "reject":
                budget_extra["execution_evidence"] = execution_evidence(
                    est, decision, effective_budget,
                )
                if decision.over == "hard":
                    # 硬限:重写也降不下来,不烧 LLM 重生成预算
                    logger.info(
                        "budget HARD hit for %r: est %d > hard cap %d",
                        state.question[:80], est.estimated_rows or -1,
                        effective_budget.hard_scan_rows,
                    )
                    return {
                        "error": (
                            "[ERR:ROW_GUARD] The EXPLAIN plan estimates "
                            f"{est.estimated_rows} rows — far beyond the "
                            f"{effective_budget.hard_scan_rows}-row hard cap. "
                            "This query would scan an unbounded result set, so "
                            "it was not executed."
                        ),
                        "columns": [],
                        "rows": [],
                        "row_count": -1,
                        **authz_extra,
                        **budget_extra,
                    }
                if decision.over != "soft":
                    # 无法估算 + 本部署配置为拒绝(§8.3 C)。**不打回重生成**:
                    # 估算不出来是因为这个方言没有解析器,不是这条 SQL 写得不好,
                    # 重写十遍还是估算不出来 —— 那只会烧掉共享修正预算。
                    logger.info(
                        "budget: 无估算依据且 on_unestimable=reject,%r 未执行",
                        state.question[:80],
                    )
                    return {
                        "error": f"[ERR:ROW_GUARD] {decision.reason}",
                        "columns": [],
                        "rows": [],
                        "row_count": -1,
                        **authz_extra,
                        **budget_extra,
                    }
                logger.info(
                    "budget soft cap hit for %r: est %d > soft cap %d",
                    state.question[:80], est.estimated_rows or -1,
                    effective_budget.soft_scan_rows,
                )
                return _execution_failure(
                    state,
                    "[ERR:ROW_GUARD] The EXPLAIN plan estimates a result "
                    f"larger than {effective_budget.soft_scan_rows} rows. Narrow "
                    "the query (add filters / aggregation, or a LIMIT) so it "
                    "returns a bounded result set.",
                    max_retries,
                ) | authz_extra | budget_extra

            limit_applied: int | None = None
            if decision.verdict == "degrade":
                if est.degraded:
                    # R1 的落点。口径是 ``est.degraded``(**没有**估算依据,I2 ——
                    # 与答案里那个同名字段同义),不是 ``verdict="degrade"``:后者
                    # 还含画像档超软限那种「有依据、按大表处置」的降级,把它记成
                    # 「没有护栏」正是 R1 要防的那类误读(见 metrics.SQL_DEGRADED)。
                    record_sql_degraded(metric_datasource)
                # 路径 3(无法估算)是本方案改的**唯一**一处行为:存量在这里
                # 放行,现在改为「加 LIMIT 执行 + 留痕」(§5.2 / I2 / I6)。
                limited = force_limit(
                    state.sql, state.dialect or "", effective_budget.max_rows,
                )
                if limited is not None:
                    executed_sql, limit_applied = limited.sql, limited.limit
                else:
                    # I6 与 I2 的边界:加不上就不拦(不阻断链路),但**不谎称**
                    # 加了 —— ``limit_applied=None`` 配上 ``degraded=True`` 就是
                    # 「已降级、且这条没边界」的准确表达。
                    logger.warning(
                        "budget: 无法为 %r 加上 LIMIT(方言解析不了),按原样执行",
                        state.question[:80],
                    )
            budget_extra["execution_evidence"] = execution_evidence(
                est, decision, effective_budget, limit_applied=limit_applied,
            )

        result = None
        timeout_s = timeout_ms / 1000.0
        retryable = _TRANSIENT_RETRIES  # 瞬时连接抖动的小重试预算(同一条 SQL)
        retry_backoff_s = _TRANSIENT_BACKOFF_S
        while True:
            try:
                with record_span("tool.execute_sql", input=executed_sql) as span:
                    result = await asyncio.wait_for(
                        connectors.execute(executed_sql, state.datasource or None),
                        timeout=timeout_s,
                    )
                    if span is not None:
                        span.update(output={"row_count": result.row_count})
                break
            except asyncio.TimeoutError:
                # 超时未必是 SQL 的问题(慢查询/连接抖动)——残余重试预算内再试一次
                if retryable > 0:
                    retryable -= 1
                    await asyncio.sleep(retry_backoff_s)
                    retry_backoff_s = min(retry_backoff_s * 2, 4.0)
                    continue
                # 真的放弃了才问终止(I4 / §10):适配器侧已经发过一次取消
                # (wait_for 取消的就是它),这里要的是**那一次的结果**——
                # 它在存量代码里被吞进了 logger.debug,所以「发没发出去、
                # 这个方言支不支持」在答案里一个字都没有。
                timeout_evidence = budget_extra.get("execution_evidence")
                if timeout_evidence is not None:
                    kill = await _terminate(terminator, state, connectors)
                    timeout_evidence["terminated"] = "timeout"
                    timeout_evidence["kill"] = kill
                    # 与证据同一份结论,不另问一次终止(§10:不重试 kill)。
                    # 没试过就没有结论 —— ``""`` 由 record_sql_kill 自己挡掉。
                    record_sql_kill(metric_datasource, kill)
                elif terminator is not None:
                    # 没装成本轨就没有证据块可落(与画像、报价同开关)。终止照发,
                    # 但要留痕 —— 静默丢弃一个「查询被主动终止」的事实,
                    # 正是 I2 要禁止的那类沉默
                    logger.warning(
                        "execute_sql: 已终止超时查询但无执行证据块可落(未装配成本轨)"
                    )
                return _execution_failure(
                    state,
                    "[ERR:SQL_TIMEOUT] "
                    + L(
                        state.lang,
                        f"查询超时（{timeout_ms}ms）",
                        f"Query timed out after {timeout_ms}ms",
                    ),
                    max_retries,
                ) | authz_extra | budget_extra
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # 瞬态连接错误(断连/不可达/协议重置)与 SQL 错误(语法/列不存在)
                # 判别:前者重跑同一 SQL 大概率恢复,后者重跑必死——只在瞬时
                # 错误上烧小重试,SQL 错误直接喂回错误反馈(同旧语义)。
                # 反馈文本一律带 [ERR:<class>] 前缀,供 analyze_error 预分流。
                if retryable > 0 and is_transient(e):
                    retryable -= 1
                    await asyncio.sleep(retry_backoff_s)
                    retry_backoff_s = min(retry_backoff_s * 2, 4.0)
                    continue
                return _execution_failure(
                    state, tag_error(str(e), context="sql"), max_retries,
                ) | authz_extra

        # 血缘捕获:成功执行的查询记录为消费方(downstream)事实。
        # 失败永远不记录(重试轮的正确 SQL 由最终成功的一次独占)。
        # 记的是**真正执行的那条**(降级时带 LIMIT)—— 血缘要能解释「线上到底
        # 跑的是什么」,记生成的原串会让降级永远查不出来。
        if lineage is not None:
            try:
                ds = state.datasource or connectors.default_name or ""
                if ds:
                    await lineage.record_query(executed_sql, ds, state.dialect)
            except Exception as e:  # 血缘失败绝不阻断查询链路
                logger.warning("lineage record failed: %s", e)

        # 数据截止时间(§8.4 / I5):与成本估算共用同一份画像。放在**执行之后**
        # —— 它回答不了「要不要执行」,只有结果要交付时才需要说清「这份数据
        # 截止到什么时候」。故障不阻断(I6),但也**不冒充「没查过」**:试过了,
        # 答案是「无从判断」。
        evidence = budget_extra.get("execution_evidence")
        if evidence is not None:
            evidence["data_as_of"], evidence["as_of_basis"] = await _freshness(
                profiles, state, executed_sql,
            )

        result_limits = get_result_limits()
        return {
            "columns": result.columns,
            # 查询结果上限(管理台可配,默认 1000 行):防止超大结果集撑爆
            # 内存/传输;row_count 保留真实总数用于展示与提示。
            "rows": result.rows[:result_limits.max_rows],
            "row_count": result.row_count,
            "execution_time_ms": result.execution_time_ms,
            "error_feedback": "",  # success clears previous feedback
            **authz_extra,
            **budget_extra,
        }

    return execute_sql


# 瞬时连接错误的小重试预算(同一条 SQL,不烧 LLM 生成预算)。
# 针对本地/远程 MySQL 抖动:断连、不可达、协议重置等瞬态错误重跑大概率
# 恢复;SQL 自身错误(语法/缺列)重跑必死,不做无谓的 sleep。
_TRANSIENT_RETRIES = 2
_TRANSIENT_BACKOFF_S = 0.5

#: 终止服务报不出来时记的值。**「试了没成」而不是「不支持」**:终止器自己炸了
#: 不等于这个方言没有终止能力,记成 ``kill_unsupported`` 会把一个运行时故障
#: 说成一个静态事实,I4 的证据就失真了。
_KILL_FAILED = "kill_failed"


async def _terminate(
    terminator: Any, state: WorkflowState, connectors: Any,
) -> str:
    """问终止服务那条查询有没有被主动终止(设计 §7.3 / §10);永不抛。

    ``""`` = 没装终止器(**没试过**):与「试过了,这个方言不支持」是两件事,
    不能合并 —— 合并之后「我们根本没接这条轨」会伪装成「这个库不支持」,
    而这两句话对运维的含义完全相反。

    数据源名沿用血缘那处同一个解析(``state.datasource`` → 默认源):证据里
    要写清**终止的是哪个源**,让空串一路漏进去会把「默认源」和「不知道是谁」
    记成同一个样子。

    ``QueryTerminator`` 自己承诺永不抛,这里仍兜一层(I6):调用点已经在
    「查询超时」的收尾路径上,再让一个护栏异常把它换成一个别的错误,是最没
    有收益的失败方式。
    """
    if terminator is None:
        return ""
    try:
        datasource = state.datasource or connectors.default_name or ""
        result = await terminator.terminate(datasource)
        return str(getattr(result, "kind", "") or _KILL_FAILED)
    except Exception as e:
        logger.warning("execute_sql: 终止服务故障(%s),按未确认处理", e)
        return _KILL_FAILED


async def _judge_budget(
    budget: BudgetService, state: WorkflowState,
) -> tuple[CostEstimate, BudgetDecision, ExecutionBudget]:
    """估算 + 判定;护栏自身故障 → 保守降级,不 raise(设计 §4 I6)。

    I6 是 I2 的**边界**而不是它的反面:可以「不拦」,不可以「不拦还说没事」。
    所以这里的兜底是**保守降级**而不是放行 —— 守卫自己炸了的时候,恰恰是最
    没有理由相信这次查询便宜的时候。

    兜底也要给出预算对象(而不是让服务自己报):服务已经不自证可用了,再回头
    问它阈值就等于把炸掉的那部分又用了一次。默认 :class:`ExecutionBudget` 是
    配置里那套保守值,方向正确。
    """
    try:
        est = await budget.estimate(
            state.datasource or "", state.sql, state.dialect or "",
            # 表名是元数据画像档的**入口**:画像按表行数求和,不知道表名就无从
            # 查起。解不出表名(``None``)或解析失败都不是问题 —— 那一档本来就
            # 会跳过,后面还有保守预算接着。
            #
            # 用 authz 那份 ``referenced_tables`` 而不是另写一个:两处对「SQL 触及
            # 了哪些表」的判断必须一致,不一致的那天会出现「授权认为查了 A 表、
            # 成本估算认为没查」这种谁也说不清的账。代价是二次 sqlglot 解析,
            # 毫秒级。
            referenced_tables(state.sql, state.dialect or ""),
        )
        return est, budget.decide(est), budget.budget
    except Exception as e:
        logger.warning("budget track failed (%s) — 降级执行(conservative)", e)
        est = CostEstimate(None, None, "conservative", True, {"error": str(e)})
        return (
            est,
            BudgetDecision("degrade", "", f"预算判定不可用({e}),按保守预算降级执行。"),
            ExecutionBudget(),
        )


async def _freshness(
    profiles: Any, state: WorkflowState, executed_sql: str,
) -> tuple[str | None, str]:
    """(数据截止时间, 口径);未装配 → ``(None, "")``,故障 → ``(None, "unknown")``。

    问的表名取自**真正执行的那条**(降级时带 LIMIT)—— 与血缘同一条理由:证据
    要能解释「线上到底跑的是什么」。LIMIT 不改写所引用的表,但这一条纪律不该
    因为「这次恰好没差」而放松。

    I6:画像查不动是常事(远端库慢/后端抖),不该把已经拿到结果的查询一起带走。
    """
    if profiles is None:
        return None, ""
    try:
        fresh = await profiles.freshness(
            state.datasource or "", referenced_tables(executed_sql, state.dialect or ""),
        )
        return getattr(fresh, "as_of", None), str(getattr(fresh, "basis", "") or "unknown")
    except Exception as e:
        logger.warning("freshness lookup failed (%s) — 答案按「无从判断」标注", e)
        return None, "unknown"


def _authz_message(decision) -> str:
    """授权拒绝 → 用户可见文案,带 ``[ERR:<class>]`` 前缀供 analyze_error 分流。

    文案刻意**不复述 SQL、不列声明之外的全部表名**(``table`` 那档除外,它要
    告诉人「哪张表被挡了」才有用) —— 拒绝信息不该顺带泄漏被拒者的可见范围。
    """
    tag = decision.error_tag()
    if decision.reason == "no_principal":
        detail = (
            "This run carries no authenticated identity, so the query was not "
            "executed. Authorization is checked before execution, not at the "
            "route — an execution path without an identity has no basis to "
            "authorize against."
        )
    elif decision.reason == "datasource":
        detail = (
            "This identity is not authorized for the requested datasource, so "
            "the query was not executed."
        )
    elif decision.reason == "table":
        detail = (
            "This identity is not authorized to query: "
            f"{', '.join(decision.narrowed_tables)}. The query was not executed."
        )
    else:
        detail = (
            "The query could not be parsed, so it could not be authorized; it "
            "was not executed."
        )
    return f"[ERR:{tag}] {detail}"


def _is_transient(exc: BaseException) -> bool:
    """判别瞬时连接类异常(重试可恢复) vs SQL 错误(重试无意义)。

    委托给确定性错分器(services/errors):只认可 DS_TRANSIENT / RATE_LIMIT
    两类连接层故障,语法/缺列/权限等一律 False(保守——绝不把语法错误
    当瞬态去重试)。基于异常类型 + 错误文本双重信号。
    """
    return is_transient(exc)


def _execution_failure(
    state: WorkflowState, message: str, max_retries: int,
) -> dict[str, Any]:
    """Feed the error back to gen_sql, or degrade when the budget is spent."""
    if budget_exhausted(state.retry_count, max_retries):
        return {"error": message}
    return {
        "error_feedback": message,
        "retry_count": state.retry_count + 1,
        "correction_history": [message],
        # 清掉执行产物并标记"本轮未执行":rows/columns 可能是上一轮成功的
        # 残留——analyze_error 的回归检查用 row_count == -1 区分执行错误
        # (结果集签名无意义)与执行后的规则/裁决失败(签名可比)。
        "columns": [],
        "rows": [],
        "row_count": -1,
    }


def _compile_drift_failure(state: WorkflowState, max_retries: int) -> dict[str, Any]:
    """编译通道照抄校验失败:带权威 SQL 打回 gen_sql;预算耗尽才降级。

    与 _execution_failure 共用修正预算与「本轮未执行」语义(row_count == -1),
    偏离重生成同样受 analyze_error 的 versions 回归链约束。
    """
    message = (
        f"{COMPILE_DRIFT_TAG} The generated SQL does not reproduce the "
        "authoritative compiled SQL (semantic-first deterministic channel). "
        "Regenerate it byte-for-byte from the compiled SQL in the plan:\n"
        f"```sql\n{state.compiled_sql}\n```"
    )
    return _execution_failure(state, message, max_retries)
