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

from trove.core.metrics import render_metrics
from trove.core.types import QueryResult
from trove.services.sql.budget import BudgetService, ExecutionBudget, describe_cost
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


def _profile_budget(spy, profile, **over) -> BudgetService:
    """P2:第 1 档(EXPLAIN)拿不到解析器,由**元数据画像**供第 2 档。

    这正是 sqlite / clickhouse 今天的样子 —— 也是 P2 存在的理由:那两个方言在
    P1 之后**每条查询**都落保守预算,``degraded`` 恒为真,信号退化成噪声。
    """
    return BudgetService(
        ExecutionBudget(max_rows=1000, **over),
        explain=spy.explain,
        parse_explain=lambda dialect, res: None,
        metadata=profile,
    )


class _ProfileSpy:
    """记录画像被问了什么(数据源 + 涉及的表),并按设定作答。

    表名是**从 SQL 里解出来的** —— 画像按表行数求和,不知道表名就无从查起。
    """

    def __init__(self, rows=None) -> None:
        self.rows = rows
        self.asked: list[tuple[str, list[str]]] = []

    def __call__(self, datasource, tables):
        self.asked.append((datasource, sorted(tables or [])))
        return self.rows


# ── 第 2 档:元数据画像(P2)──────────────────────────────

class TestMetadataTier:
    """画像档接上之后,**小表查询不再被降级** —— 这是 P2 真正买到的东西。

    P1 之后 sqlite/clickhouse 的每条查询都走保守预算,``degraded`` 恒为真。一个
    恒为真的字段不是信号,是噪声:没人会去看它。有了画像,只有真的估不出来时
    才降级。
    """

    async def test_the_sql_tables_are_extracted_and_asked_about(self):
        """表名从 SQL 里解出来再问画像 —— 顺带钉住数据源是**当前这一轮**的。

        多数据源部署里漏掉数据源,A 库的行数会被当成 B 库的(与
        ``BudgetService._from_explain`` 同一条理由)。
        """
        spy = _SpyConnectors()
        profile = _ProfileSpy(rows=100)
        node = make_execute_sql(spy, budget=_profile_budget(spy, profile))

        state = _state(
            datasource="analytics",
            sql="SELECT s.name FROM students s JOIN grades g ON s.id = g.sid",
        )
        result = await node(state)

        assert profile.asked == [("analytics", ["grades", "students"])]
        assert result["execution_evidence"]["source"] == "metadata"

    async def test_a_small_table_is_not_degraded_at_all(self):
        """画像说这张表只有 100 行 → 干净放行,不加 LIMIT、不记 degraded。

        执行下去的 SQL 与生成的那条**完全一致** —— 「没降级」这件事在
        connector 收到的那条串上看得见。
        """
        spy = _SpyConnectors()
        node = make_execute_sql(
            spy, budget=_profile_budget(spy, _ProfileSpy(rows=100)),
        )
        result = await node(_state())

        ev = result["execution_evidence"]
        assert ev["verdict"] == "allow"
        assert ev["degraded"] is False
        assert ev["limit_applied"] is None
        assert spy.executed == ["SELECT name FROM students"]

    async def test_a_big_table_degrades_with_a_limit_not_a_regeneration(self):
        """画像估出 6 亿行(超软限、未超硬限)→ 加 LIMIT 执行,**不打回重生成**。

        打回重生成在这里是个不收敛的环:画像看不见 LIMIT,重写十遍估值不变。
        """
        spy = _SpyConnectors()
        node = make_execute_sql(
            spy, budget=_profile_budget(spy, _ProfileSpy(rows=600_000_000)),
        )
        result = await node(_state())

        ev = result["execution_evidence"]
        assert ev["verdict"] == "degrade"
        assert ev["limit_applied"] == 1000
        assert "LIMIT 1000" in spy.executed[0].upper()
        # 没有回写 error_feedback = 没有打回 gen_sql(成功路径会把它清空)
        assert result["error_feedback"] == ""

    async def test_over_the_hard_cap_is_terminal(self):
        """画像是**粗**估算,但 1B 行这个量级不需要精确 —— 超硬限直接拒。"""
        spy = _SpyConnectors()
        node = make_execute_sql(
            spy, budget=_profile_budget(spy, _ProfileSpy(rows=5_000_000_000)),
        )
        result = await node(_state())

        assert result["error"].startswith("[ERR:ROW_GUARD]")
        assert spy.executed == []  # 没有落库

    async def test_unparseable_sql_falls_through_to_conservative(self):
        """解不出表名(方言语法太怪)→ 画像无从查起 → 保守预算,**不是放行**。

        这条是 P2 与 P1 的接缝:第 2 档够不着不等于管道断了,后面的第 3 档还在。
        """
        spy = _SpyConnectors()
        profile = _ProfileSpy(rows=100)
        node = make_execute_sql(spy, budget=_profile_budget(spy, profile))

        result = await node(_state(sql="SELECT FROM WHERE (("))

        assert profile.asked == []  # 没表名可问
        ev = result["execution_evidence"]
        assert ev["source"] == "conservative"
        assert ev["degraded"] is True


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


# ── 答案里的 data_as_of / scanned_rows(设计 §6.2 / §8.4)────────


class _FreshnessSpy:
    """画像服务的**新鲜度那一半**:记录问了哪几张表,按设定作答。

    ``estimate_rows`` 与 ``freshness`` 在生产里是同一个 ``ProfileService``
    的两个方法(共用一份 TTL 缓存,见 ``graphs._build_profile``)—— 这里拆成
    两个对象只是为了让每条断言只钉一件事。
    """

    def __init__(self, result=None, *, boom: bool = False) -> None:
        self.result = result
        self.boom = boom
        self.asked: list[tuple[str, list[str]]] = []

    async def freshness(self, datasource, tables):
        self.asked.append((datasource, sorted(tables or [])))
        if self.boom:
            raise RuntimeError("profile backend down")
        from trove.services.datasource.profile import DatasetFreshness

        return self.result or DatasetFreshness(datasource=datasource)


class TestFreshnessEvidence:
    """I5:``data_as_of`` 要么是真的(带口径),要么是 ``null`` + 「无从判断」。

    三个状态必须分得开,因为下游要据此渲染三句不同的话:

    * ``as_of_basis=""`` —— **没查过**(没装画像)。不能渲染成「无从判断」:
      那是在说「我查了,查不到」,而我们根本没查。
    * ``as_of_basis="unknown"`` —— 查过了,**无从判断**(I5 的落点)。
    * ``as_of_basis="latest_partition"`` —— 有值,**且必须跟口径一起展示**
      (R5:值的语义靠口径限定,单看值会被读成「数据已更新到此刻」)。
    """

    async def test_data_as_of_arrives_with_the_basis_that_produced_it(self):
        """有依据 → 值 + 口径一起落进证据,并问的是**这一轮的数据源**。"""
        spy = _SpyConnectors()
        fresh = _FreshnessSpy()
        from trove.services.datasource.profile import DatasetFreshness

        fresh.result = DatasetFreshness(
            datasource="analytics", as_of="2026-09-01", basis="latest_partition",
        )
        node = make_execute_sql(
            spy, budget=_profile_budget(spy, _ProfileSpy(rows=10)), profiles=fresh,
        )
        result = await node(_state(
            datasource="analytics",
            sql="SELECT s.name FROM students s JOIN grades g ON s.id = g.sid",
        ))

        ev = result["execution_evidence"]
        assert fresh.asked == [("analytics", ["grades", "students"])]
        assert ev["data_as_of"] == "2026-09-01"
        assert ev["as_of_basis"] == "latest_partition"

    async def test_unknown_freshness_is_null_and_never_the_query_time(self):
        """I5 的正题:查过了但没有依据 → ``null`` + ``"unknown"``。

        绝不能拿「查询时刻」冒充 —— 那会让用户以为数据新鲜到刚刚,而事实是
        我们**不知道**它截止到什么时候。这里断言的就是「不是那个时刻」。
        """
        spy = _SpyConnectors()
        fresh = _FreshnessSpy()  # DatasetFreshness(datasource=..., as_of=None)
        node = make_execute_sql(
            spy, budget=_profile_budget(spy, _ProfileSpy(rows=10)), profiles=fresh,
        )
        result = await node(_state())

        ev = result["execution_evidence"]
        assert fresh.asked, "装配了画像却不问,和没装一样"
        assert ev["data_as_of"] is None
        assert ev["as_of_basis"] == "unknown"
        assert "2026" not in str(ev["data_as_of"])  # 任何时刻都不是答案

    async def test_without_a_profile_service_nothing_is_claimed(self):
        """没装画像 = 没查过。三个状态里的第一个,不能和「无从判断」混同。"""
        spy = _SpyConnectors()
        node = make_execute_sql(spy, budget=_profile_budget(spy, _ProfileSpy(rows=10)))
        result = await node(_state())

        ev = result["execution_evidence"]
        assert ev["data_as_of"] is None
        assert ev["as_of_basis"] == ""

    async def test_a_broken_freshness_lookup_does_not_block_the_query(self):
        """I6:画像是**增强**,自己炸了不该把查询一起带走(§10)。

        但也不能因此变成「没查过」—— 我们确实试过了,答案是「无从判断」。
        """
        spy = _SpyConnectors()
        node = make_execute_sql(
            spy,
            budget=_profile_budget(spy, _ProfileSpy(rows=10)),
            profiles=_FreshnessSpy(boom=True),
        )
        result = await node(_state())

        assert spy.executed == ["SELECT name FROM students"]
        assert result["row_count"] == 1
        ev = result["execution_evidence"]
        assert ev["data_as_of"] is None
        assert ev["as_of_basis"] == "unknown"

    async def test_returned_rows_are_not_passed_off_as_scanned_rows(self):
        """I7/A5:两者分开落库 —— 返回行数**不是**扫描量的替代品。

        把 ``result.row_count`` 记成 ``scanned_rows`` 会让 P4 之后的校准
        (估算 vs 实际)建在一个系统性偏小的数上:扫了一亿行、聚合出三行,
        差值会把估算器一路往小调 —— 正是本能力要防的那类「错得像个答案」。

        适配器报不出扫描量时,这里就必须是 ``None``(说不出就知道不知道)。
        """
        spy = _SpyConnectors()
        node = make_execute_sql(spy, budget=_profile_budget(spy, _ProfileSpy(rows=10)))
        result = await node(_state())

        ev = result["execution_evidence"]
        assert ev["scanned_rows"] is None
        assert result["row_count"] == 1  # 真返回了一行 —— 但没有拿它冒充
        assert ev["estimated_rows"] == 10
        assert ev["estimated_rows"] != ev["scanned_rows"] or ev["scanned_rows"] is None


# ── 超时后的主动终止(P4 / 设计 §7.3 / §10 / I4)─────────

class _TimeoutConnectors(_SpyConnectors):
    """一条永远跑不完的查询 —— ``wait_for`` 会掐掉它,适配器侧收到取消。"""

    async def execute(self, sql, datasource=None):
        self.executed.append(sql)
        import asyncio

        await asyncio.sleep(60)


class _TerminatorSpy:
    """记录「被要求终止谁」,并按设定报一种结果(§7.3 / §10)。"""

    def __init__(self, kind: str = "kill_sent", *, boom: bool = False) -> None:
        self.kind = kind
        self.boom = boom
        self.asked: list[str] = []

    async def terminate(self, datasource):
        self.asked.append(datasource)
        if self.boom:
            raise RuntimeError("terminator exploded")
        from trove.services.sql.terminate import TerminationResult

        return TerminationResult(str(datasource or ""), self.kind)


def _failure_message(update) -> str:
    """这条失败去了哪:修正预算内进 ``error_feedback``,耗尽才落 ``error``。

    「超时」这件事本身与它走哪条路无关 —— 断言要认得两个桶,否则测试钉的是
    修正预算而不是超时语义。
    """
    return update.get("error") or update.get("error_feedback", "")


def _timeout_node(spy, terminator, monkeypatch, **over):
    """超时路径的节点:重试预算与退避清零,只留「超时 → 收尾」这一段。"""
    from trove.workflow.nodes import execute_sql as mod

    monkeypatch.setattr(mod, "_TRANSIENT_RETRIES", 0)
    monkeypatch.setattr(mod, "_TRANSIENT_BACKOFF_S", 0.0)
    return make_execute_sql(
        spy,
        timeout_ms=1,
        max_retries=over.pop("max_retries", 10),
        budget=_no_basis_budget(spy),
        terminator=terminator,
        **over,
    )


class TestTerminationOnTimeout:
    """I4:**超时必须主动终止**,并且这件事要能被看见(§10)。

    设计原本让 ``QueryTerminator`` 自己发 kill,而超时路径上适配器的
    ``execute`` 已经发过了(``asyncio.wait_for`` 取消的正是它)—— 照设计做
    就是同一条查询发两次 kill,而 §10 明说不重试 kill。所以这一层的职责是
    **如实报告那一次的结果**,而不是再发一次。
    """

    async def test_a_timed_out_query_records_how_it_was_terminated(
        self, monkeypatch,
    ):
        spy = _TimeoutConnectors()
        term = _TerminatorSpy("kill_sent")
        node = _timeout_node(spy, term, monkeypatch)

        result = await node(_state())

        assert _failure_message(result).startswith("[ERR:SQL_TIMEOUT]")
        assert term.asked == [DEFAULT], "超时却不问终止,等于没接"
        ev = result["execution_evidence"]
        assert ev["terminated"] == "timeout"
        assert ev["kill"] == "kill_sent"

    async def test_a_dialect_that_cannot_be_killed_says_exactly_that(
        self, monkeypatch,
    ):
        """§10 那格:不支持就记 ``kill_unsupported`` + 放弃等待 ——
        **不是** ``kill_failed``(那是「试了没成」,要人去看)。
        """
        spy = _TimeoutConnectors()
        node = _timeout_node(spy, _TerminatorSpy("kill_unsupported"), monkeypatch)

        result = await node(_state())

        assert result["execution_evidence"]["kill"] == "kill_unsupported"

    async def test_a_failed_kill_is_recorded_not_hidden(self, monkeypatch):
        spy = _TimeoutConnectors()
        node = _timeout_node(spy, _TerminatorSpy("kill_failed"), monkeypatch)

        result = await node(_state())

        assert result["execution_evidence"]["kill"] == "kill_failed"

    async def test_termination_waits_until_the_retry_budget_is_spent(
        self, monkeypatch,
    ):
        """重试还在预算内就发终止 = 自己打自己下一次尝试。

        §10 的「不重试 kill」在这里是同一个道理的两面:只在**真的放弃**那一次
        终止,不在每一次超时都终止。
        """
        spy = _TimeoutConnectors()
        term = _TerminatorSpy()
        from trove.workflow.nodes import execute_sql as mod

        monkeypatch.setattr(mod, "_TRANSIENT_RETRIES", 2)
        monkeypatch.setattr(mod, "_TRANSIENT_BACKOFF_S", 0.0)
        node = make_execute_sql(
            spy, timeout_ms=1, budget=_no_basis_budget(spy), terminator=term,
        )

        result = await node(_state())

        assert _failure_message(result).startswith("[ERR:SQL_TIMEOUT]")
        assert len(spy.executed) == 3, "三次尝试都用掉了才放弃"
        assert len(term.asked) == 1, "每一次超时都终止会把下一次尝试一起杀掉"

    async def test_without_a_terminator_nothing_is_claimed(self, monkeypatch):
        """没装终止器 = **没试过**。第三个状态,不能写成「不支持」也不能写成
        「失败」—— 那两种都是查过之后的结论。
        """
        spy = _TimeoutConnectors()
        node = _timeout_node(spy, None, monkeypatch)

        result = await node(_state())

        assert _failure_message(result).startswith("[ERR:SQL_TIMEOUT]")
        ev = result["execution_evidence"]
        assert ev["terminated"] == "timeout"
        assert ev["kill"] == ""

    async def test_a_broken_terminator_does_not_replace_the_timeout(
        self, monkeypatch,
    ):
        """I6:终止是**收尾**,它自己炸了不能把「查询超时」换成别的错。"""
        spy = _TimeoutConnectors()
        node = _timeout_node(spy, _TerminatorSpy(boom=True), monkeypatch)

        result = await node(_state())

        assert _failure_message(result).startswith("[ERR:SQL_TIMEOUT]")
        assert result["execution_evidence"]["kill"] == "kill_failed"

    async def test_a_successful_query_never_claims_termination(self):
        """跑完的查询没有「终止」这回事:两个字段都必须是「没有」。"""
        spy = _SpyConnectors()
        node = make_execute_sql(spy, budget=_no_basis_budget(spy))

        result = await node(_state())

        ev = result["execution_evidence"]
        assert ev["terminated"] is None
        assert ev["kill"] == ""


# ── 执行画像指标(设计 §9.2 / R1 · P5)────────────────────


def _counter(name: str, **labels: str) -> float | None:
    """从 ``/v1/metrics`` 的文本里取一条序列的值;这条序列不存在 → ``None``。

    读文本而不是私有 ``_value``:与运维抓到的是同一份东西(顺带钉住标签真的
    渲染出来了)。**每条用例用各自的数据源名**:注册表是进程级的,同一个名字
    会被别的用例累加,而「1」和「上一次留下的 1」在断言上长得一样。
    """
    text = render_metrics().decode()
    for line in text.splitlines():
        if not line.startswith(name + "{"):
            continue
        if all(f'{k}="{v}"' in line for k, v in labels.items()):
            return float(line.rsplit(" ", 1)[1])
    return None


class TestExecutionMetrics:
    """三条计数器**只在执行节点**落账(设计 §9.2 / R1)。

    记录点在节点而不在 ``BudgetService`` / ``describe_cost``:同一套判定在「生成
    前问价」时也会发生(问价工具与执行节点逐字调用同一个 ``estimate``/``decide``),
    在服务层记会把**问价的**和**真跑了的**混成一个数 —— 于是「这条查询到底跑没
    跑」再也答不上来,而那正是上线后用 estimated/scanned 校准估算器时唯一的分母
    (R2)。节点记才等于「这条查询落库了」。
    """

    async def test_a_degraded_execution_is_counted_on_both_counters(self):
        """降级执行的**两侧**都记:判定(conservative + degrade)与执行(degraded)。

        R1 点名的就是第二个 —— 「降级被当成没护栏」的一半是「看不见」。
        """
        spy = _SpyConnectors()
        node = make_execute_sql(spy, budget=_no_basis_budget(spy))

        await node(_state(datasource="p5_degrade_db"))

        assert _counter("trove_sql_degraded_total", datasource="p5_degrade_db") == 1
        assert _counter(
            "trove_sql_budget_decisions_total",
            datasource="p5_degrade_db", source="conservative", verdict="degrade",
        ) == 1

    async def test_an_allow_execution_is_not_a_degradation(self):
        """有依据、放行 → 决策落在 explain + allow,降级计数一条都不该有。

        降级计数若恒记就不再是 R1 要的信号 —— 一个恒为真的字段没人会看(与 P2
        修掉的「sqlite 上每条查询都 degraded」同一条理由)。
        """
        spy = _SpyConnectors(explain_rows=1_000)
        node = make_execute_sql(spy, budget=_mysql_budget(spy))

        await node(_state(dialect="mysql", datasource="p5_allow_db"))

        assert _counter("trove_sql_degraded_total", datasource="p5_allow_db") is None
        assert _counter(
            "trove_sql_budget_decisions_total",
            datasource="p5_allow_db", source="explain", verdict="allow",
        ) == 1

    async def test_a_reject_is_a_decision_but_never_a_degradation(self):
        """``on_unestimable=reject`` 的部署:判了(conservative + reject)但**没跑**。

        决策计数含没跑的那些(否则 reject 这一档永远不出现在分布里);
        降级计数只计**执行事实** —— 记在决策那一步会把「拒绝了」说成「降级跑了」,
        而这两句话对运维的含义相反(前者什么也没发生,后者一条无依据的查询落了库)。
        """
        spy = _SpyConnectors()
        node = make_execute_sql(
            spy, budget=_no_basis_budget(spy, on_unestimable="reject"),
        )

        await node(_state(datasource="p5_reject_db"))

        assert spy.executed == []
        assert _counter(
            "trove_sql_budget_decisions_total",
            datasource="p5_reject_db", source="conservative", verdict="reject",
        ) == 1
        assert _counter("trove_sql_degraded_total", datasource="p5_reject_db") is None

    async def test_a_metadata_estimate_over_the_soft_cap_is_not_counted_here(self):
        """画像档超软限 → ``verdict=degrade``,但**不是** R1 说的那种降级。

        这是本能力里一个有意收窄的口径:``trove_sql_degraded_total`` 对齐的是答案
        里那个同名字段 ``degraded``(``est.degraded`` —— 「**没有**估算依据」,I2),
        而不是 ``verdict=degrade``(「按既有依据降级执行」)。两者在画像档上分叉:
        ``verdict=degrade`` 混合了两种成因 —— (1) 一点依据都没有(保守预算,I2),
        (2) 画像说这是张大表(P2 新接的依据,降级是因为**有**依据才降)。

        混在一起会把「有依据、按大表处置」记成「没有护栏」—— 而后者才是 R1 要
        人看见的东西。分开之后两条都还在:前者看这个计数器,后者看
        ``trove_sql_budget_decisions_total{source="metadata",verdict="degrade"}``。
        """
        spy = _SpyConnectors()
        node = make_execute_sql(
            spy, budget=_profile_budget(spy, _ProfileSpy(rows=600_000_000)),
        )

        await node(_state(datasource="p5_metadata_degrade_db"))

        assert _counter("trove_sql_degraded_total", datasource="p5_metadata_degrade_db") is None
        assert _counter(
            "trove_sql_budget_decisions_total",
            datasource="p5_metadata_degrade_db", source="metadata", verdict="degrade",
        ) == 1

    async def test_a_kill_result_is_counted_when_there_was_one(self, monkeypatch):
        """超时 + 有终止结论 → 按 ``result`` 计数(§10 / I4)。

        这是 R1 的兄弟:降级是「没护栏」看得见,终止是「放弃了之后发生了什么」
        看得见 —— ``kill_failed`` 与 ``kill_unsupported`` 混成一个数,运维看到
        的东西完全不同。
        """
        spy = _TimeoutConnectors()
        node = _timeout_node(spy, _TerminatorSpy("kill_failed"), monkeypatch)

        await node(_state(datasource="p5_kill_db"))

        assert _counter(
            "trove_sql_kill_total", datasource="p5_kill_db", result="kill_failed",
        ) == 1

    async def test_a_kill_that_was_never_attempted_is_not_counted(self, monkeypatch):
        """没装终止器 → ``kill == ""`` → **不成一条序列**。

        「没试过」不是终止结果的一种:记一个空串会让「我们没接这条轨」混进结果
        分布,而它与「这个方言不支持」对运维的含义相反(前者是我们没接,后者
        是库不给这个能力)。这里连``result=""`` 那条都不许出现。
        """
        spy = _TimeoutConnectors()
        node = _timeout_node(spy, None, monkeypatch)

        await node(_state(datasource="p5_no_kill_db"))

        assert _counter("trove_sql_kill_total", datasource="p5_no_kill_db") is None

    async def test_asking_for_a_price_is_not_an_execution(self):
        """问价(``describe_cost``)与执行共用同一套判定,但只有后者进执行计数。

        本能力最容易接错的一处:``estimate`` / ``decide`` 是同一份代码,记录点若
        放在服务层,「生成前问一次价」就会和「真跑了一条查询」进同一个数 —— 而
        问价**不碰数据库**,真跑了的那条可能打爆生产库,两者的运维含义相反。

        第二半跑同一个数据源:证明上面那两条否定不是数据源名写错导致的空断言。
        """
        spy = _SpyConnectors()
        budget = _no_basis_budget(spy)

        quote = await describe_cost(
            "SELECT name FROM students", budget=budget,
            datasource="p5_quote_db", dialect="sqlite",
        )
        assert quote["verdict"] == "degrade", "判定确实发生了(否则这条用例是空的)"
        assert _counter("trove_sql_budget_decisions_total", datasource="p5_quote_db") is None
        assert _counter("trove_sql_degraded_total", datasource="p5_quote_db") is None

        node = make_execute_sql(spy, budget=budget)
        await node(_state(datasource="p5_quote_db"))

        assert _counter("trove_sql_degraded_total", datasource="p5_quote_db") == 1
