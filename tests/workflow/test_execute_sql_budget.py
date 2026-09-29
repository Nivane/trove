"""``execute_sql`` 的成本轨接入(执行画像设计 §5.2 / §9.2 P1)。

存量三道成本护栏(EXPLAIN 解析 / 方言支持 / 估算可得)**全在往「放行」倒**:
解析不出 → 放行,方言没有解析器 → 放行,EXPLAIN 报错 → 放行。三处叠加的结果是
一条扫全表的 SELECT 有零个成本上限(设计 §2.2 G1)。

本文件钉住接入后的**方向**:估算不出来时不再放行,而是**降级执行 + 留痕**。
这是本方案改的唯一一处行为,所以这里最要紧的两条断言是:

* 执行下去的 SQL 真的带了边界(I2 不是只记一笔日志);
* 降级这件事**在返回值里看得见**(R1:看不见的降级等于没有降级)。

**``state.sql`` 保留生成的原串,不写回加了 LIMIT 的版本** —— 理由见
``TestExecutedSqlIsNotTheShownSql``:LIMIT 是部署级执行策略,不是这个问题的答案;
把它烤进 ``state.sql`` 会让同一份数据上的同一个问题在不同部署里产出不同的 SQL
文本,而 ``state.sql`` 还要被血缘与已验证查询资产(能力③)消费。
"""

from __future__ import annotations

from trove.core.types import QueryResult
from trove.services.sql.budget import BudgetService, ExecutionBudget
from trove.workflow.nodes.execute_sql import make_execute_sql
from trove.workflow.state import WorkflowState

DEFAULT = "test_db"


def _qresult(rows, columns=("rows",)):
    return QueryResult(columns=list(columns), rows=rows, row_count=len(rows),
                       execution_time_ms=1)


class _SpyConnectors:
    """记录**真正执行下去**的 SQL —— 「加了 LIMIT」与「记了一笔加了 LIMIT」
    在返回值上长得一样,差别只在 connector 收到的那条串。"""

    default_name = DEFAULT

    def __init__(self, explain_rows=None, *, explains: bool = True) -> None:
        self.executed: list[str] = []
        self._explains = explains
        self._explain_rows = explain_rows

    async def explain(self, sql, datasource=None):
        if self._explain_rows is None:
            raise RuntimeError("this dialect has no EXPLAIN parser")
        return _qresult([(1, "ALL", "t", self._explain_rows)],
                        columns=["id", "select_type", "table", "rows"])

    async def execute(self, sql, datasource=None):
        self.executed.append(sql)
        return _qresult([[1]], columns=["n"])


def _state(**kwargs) -> WorkflowState:
    defaults = {
        "session_id": "s1",
        "question": "q",
        "sql": "SELECT name FROM students",
        "dialect": "sqlite",
    }
    defaults.update(kwargs)
    return WorkflowState(**defaults)


def _mysql_budget(spy, **over) -> BudgetService:
    """EXPLAIN 能解析的方言(mysql),用来单独验三档判定。

    接法与 ``graphs._build_budget`` 一致:第 1 档走 ``connectors.explain`` +
    默认解析器 ``row_guard.estimate_max_rows``。
    """
    return BudgetService(ExecutionBudget(max_rows=1000, **over), explain=spy.explain)


def _no_basis_budget(spy, **over) -> BudgetService:
    """sqlite / clickhouse 现状:有 adapter、**没有 EXPLAIN 解析器** → 路径 3。

    EXPLAIN 本身拿得到(``_SpyConnectors(explain_rows=...)``),只是没有解析器
    把计划文本变成行数 —— 这正是那两个方言今天的样子。
    """
    return BudgetService(
        ExecutionBudget(max_rows=1000, **over),
        explain=spy.explain,
        parse_explain=lambda dialect, res: None,
    )


# ── 降级执行:路径 3 的唯一行为变更 ────────────────────────


class TestDegradedExecution:
    """估算不可得 → 不再放行,改为「加 LIMIT 执行 + 留痕」。"""

    async def test_unestimable_executes_with_a_forced_limit(self):
        spy = _SpyConnectors()
        node = make_execute_sql(spy, budget=_no_basis_budget(spy))
        await node(_state())

        assert spy.executed == ["SELECT name FROM students LIMIT 1000"]

    async def test_degradation_is_visible_in_the_returned_state(self):
        """R1:看不见的降级等于没有降级 —— 用户侧必须能看出这一条被降级执行了。"""
        spy = _SpyConnectors()
        node = make_execute_sql(spy, budget=_no_basis_budget(spy))
        update = await node(_state())

        ev = update["execution_evidence"]
        assert ev["degraded"] is True
        assert ev["source"] == "conservative"
        assert ev["limit_applied"] == 1000
        assert ev["verdict"] == "degrade"

    async def test_already_bounded_sql_is_not_widened(self):
        """已有 LIMIT 10 → 保持 10。放宽到 1000 是借降级之名扩权。"""
        spy = _SpyConnectors()
        node = make_execute_sql(spy, budget=_no_basis_budget(spy))
        await node(_state(sql="SELECT name FROM students LIMIT 10"))

        assert "LIMIT 10" in spy.executed[0]
        assert "1000" not in spy.executed[0]

    async def test_unparseable_sql_still_runs_but_does_not_claim_a_bound(self):
        """I2 的硬要求:**拦不住可以说,不能不说**。

        ``force_limit`` 加不上(方言解析不了这条 SQL)→ I6 要求不阻断链路,
        但返回值里不能出现一个「已降级、有边界」的假象。
        """
        spy = _SpyConnectors()
        node = make_execute_sql(spy, budget=_no_basis_budget(spy))
        update = await node(_state(sql="SELECT FROM WHERE (("))

        assert spy.executed == ["SELECT FROM WHERE (("]  # I6:照跑
        ev = update["execution_evidence"]
        assert ev["degraded"] is True       # 但降级这件事说了
        assert ev["limit_applied"] is None  # 且不谎称加了边界

    async def test_on_unestimable_reject_refuses_without_executing(self):
        """§8.3 C:严格环境可选 reject —— 拒绝,且**不执行**。

        拒绝是**终局**,不打回 gen_sql:估算不出来是因为这个方言没有解析器,
        不是这条 SQL 写得不好 —— 重写十遍还是估算不出来。
        """
        spy = _SpyConnectors()
        node = make_execute_sql(spy, budget=_no_basis_budget(spy, on_unestimable="reject"))
        update = await node(_state())

        assert spy.executed == []
        # 沿用既有 [ERR:ROW_GUARD] 前缀(不新造 tag)—— analyze_error 按前缀
        # 预分流,新 tag 会被当成未知错误走通用 LLM 路径。
        assert "ROW_GUARD" in update["error"]
        assert "error_feedback" not in update  # 终局,不烧修正预算
        assert "重新生成" not in update["error"]


class TestExecutedSqlIsNotTheShownSql:
    """``state.sql`` 不写回加了 LIMIT 的版本(本文件的实现决定,设计未指定)。"""

    async def test_state_sql_stays_the_generated_one(self):
        spy = _SpyConnectors()
        node = make_execute_sql(spy, budget=_no_basis_budget(spy))
        update = await node(_state())

        assert "sql" not in update, (
            "state.sql 保留生成的原串:LIMIT 是部署级执行策略,不是这个问题的答案"
        )
        assert update["rows"] is not None  # 结果是有的

    async def test_limit_still_carried_when_sql_already_bounded(self):
        """SQL 本身已带更紧的 LIMIT → ``limit_applied`` 记的是**生效的那个**。

        记 1000 会让运维以为这条查询被放到了 1000 行,而实际只有 10 行。
        """
        spy = _SpyConnectors()
        node = make_execute_sql(spy, budget=_no_basis_budget(spy))
        update = await node(_state(sql="SELECT name FROM students LIMIT 10"))

        assert update["execution_evidence"]["limit_applied"] == 10


# ── 三档判定在节点里仍然成立(现有行为,保留)────────────


class TestBudgetVerdicts:
    async def test_estimate_under_soft_executes_unmodified(self):
        spy = _SpyConnectors(explain_rows=1_000)
        node = make_execute_sql(spy, budget=_mysql_budget(spy))
        update = await node(_state(dialect="mysql"))

        assert spy.executed == ["SELECT name FROM students"]
        ev = update["execution_evidence"]
        assert ev["degraded"] is False
        assert ev["source"] == "explain"
        assert ev["limit_applied"] is None

    async def test_over_soft_feeds_back_for_narrowing(self):
        """超软限 ≠ 直接拒 —— 打回 gen_sql 加过滤/收窄(§8.5 保留双上限)。"""
        spy = _SpyConnectors(explain_rows=100_000_000)
        node = make_execute_sql(spy, budget=_mysql_budget(spy))
        update = await node(_state(dialect="mysql"))

        assert spy.executed == []
        assert "error" not in update
        assert "ROW_GUARD" in update["error_feedback"]
        assert update["retry_count"] == 1

    async def test_over_hard_rejects_without_burning_llm_budget(self):
        spy = _SpyConnectors(explain_rows=5_000_000_000)
        node = make_execute_sql(spy, budget=_mysql_budget(spy))
        update = await node(_state(dialect="mysql"))

        assert spy.executed == []
        assert "ROW_GUARD" in update["error"]
        assert "error_feedback" not in update  # 不打回重生成


# ── I6:护栏自身故障不阻断查询 ────────────────────────────


class TestGuardFailureDoesNotBlock:
    async def test_explain_explosion_degrades_instead_of_blocking(self):
        """EXPLAIN 报错(方言不支持 / 权限不足 / 连接抖动)→ 降级执行。

        存量在这里直接放行;改造后仍然执行,但**加了边界且说了出来**。
        """
        spy = _SpyConnectors(explain_rows=None)
        node = make_execute_sql(spy, budget=_mysql_budget(spy))
        update = await node(_state(dialect="mysql"))

        assert spy.executed == ["SELECT name FROM students LIMIT 1000"]
        assert update["execution_evidence"]["degraded"] is True

    async def test_a_broken_budget_service_does_not_block_the_query(self):
        """护栏自己炸了不该让用户查不了数 —— 退到保守降级,不 raise。"""

        class _Exploding:
            async def estimate(self, *a, **k):
                raise RuntimeError("budget service is on fire")

            def decide(self, *a, **k):
                raise RuntimeError("budget service is on fire")

        spy = _SpyConnectors()
        node = make_execute_sql(spy, budget=_Exploding())
        update = await node(_state())

        assert "error" not in update
        assert spy.executed == ["SELECT name FROM students LIMIT 1000"]
        assert update["execution_evidence"]["degraded"] is True


# ── 未装配 ≠ 判过了 ───────────────────────────────────────


class TestUnassembledBudget:
    async def test_no_budget_means_no_judgement_and_no_forced_limit(self):
        """``budget=None`` = 没装这道门(嵌入场景可关),不是「判过了、放行」。

        与 ``authorizer=None`` 同一种接法:本节点不会假装自己判过。嵌入场景
        (纯 stdio 本地库)关掉它,行为与改造前逐字一致 —— 不加 LIMIT。
        """
        spy = _SpyConnectors()
        node = make_execute_sql(spy)
        update = await node(_state())

        assert spy.executed == ["SELECT name FROM students"]
        assert "execution_evidence" not in update


# ── 与授权门同框:两道门的顺序 ────────────────────────────


class TestCoexistsWithAuthorizer:
    async def test_denied_query_is_never_cost_estimated(self):
        """授权拒绝在成本轨**之前** —— 不该为一条注定不跑的 SQL 去 EXPLAIN。

        EXPLAIN 本身要落库(是查询),在一片 deny 的部署里它是一条噪声链路。
        """
        from trove.services.authz.enforcer import Authorizer

        explained: list[str] = []

        class _Spy(_SpyConnectors):
            async def explain(self, sql, datasource=None):
                explained.append(sql)
                return _qresult([(1, "ALL", "t", 1_000)],
                                columns=["id", "select_type", "table", "rows"])

        spy = _Spy()
        node = make_execute_sql(
            spy,
            budget=_mysql_budget(spy),
            authorizer=Authorizer(declared_tables=lambda _ds: None, mode="enforce"),
        )
        update = await node(_state(dialect="mysql"))

        assert "AUTHZ_NO_PRINCIPAL" in update["error"]
        assert explained == [], "被拒的 SQL 不该被 EXPLAIN"
        assert spy.executed == []
