"""``execute_sql`` 的执行前授权门(设计 §5.3 G2 / §10)。

这一层补的是 G2「执行路径零鉴权」:存量实现里授权是路由的装饰器
(``Depends(require_datasource)``),于是任何不经路由的执行入口 —— 图内节点、
API 直执行、job payload、MCP 工具 —— 都不受约束。判定挪到 SQL 落库之前。

**本文件里最要紧的一条断言不是「拒绝了」,是「没有执行」。** 拒绝一条 SQL 与
「拒绝了但还是跑了」在返回值上看起来一样,差别只在 connector 有没有被调用。
"""

from __future__ import annotations

import pytest

from trove.services.authz.enforcer import Authorizer
from trove.services.authz.policy import Principal, principal_to_wire
from trove.services.semantic_layer.models import SemanticDataset, SemanticModel
from trove.workflow.nodes.execute_sql import make_execute_sql
from trove.workflow.state import WorkflowState

DEFAULT = "test_db"


def _warn_total() -> float:
    """warn 计数器的**值**(所有数据源求和)。

    读值而不是数行:注册表是进程级的,同文件里先跑的 warn 用例已经把那条序列
    建出来了 —— 数行只会数出「序列存不存在」,而这里要问的是「这一笔记上没有」。
    ``_created`` 伴随序列不会被误取(前缀带下划线,不是 ``{``)。
    """
    from trove.core.metrics import render_metrics

    return sum(
        float(line.rsplit(" ", 1)[1])
        for line in render_metrics().decode().splitlines()
        if line.startswith("trove_authz_table_warn_total{")
    )


#: 语义层声明过的表。``students`` 在 ``sqlite_registry`` 里真实存在;
#: ``salaries`` 不在声明里 —— A3 要挡的就是后者。
_DECLARED = {"students"}


class _Result:
    def __init__(self) -> None:
        self.columns = ["n"]
        self.rows = [[1]]
        self.row_count = 1
        self.execution_time_ms = 1.0


class _SpyConnectors:
    """记录执行过的 SQL —— 「拒绝」与「拒绝了但照跑」靠它区分。"""

    default_name = DEFAULT

    def __init__(self) -> None:
        self.executed: list[str] = []

    async def execute(self, sql, datasource=None):
        self.executed.append(sql)
        return _Result()


def _state(**kwargs) -> WorkflowState:
    defaults = {
        "session_id": "s1",
        "question": "q",
        "sql": "SELECT count(*) FROM students",
        "dialect": "sqlite",
    }
    defaults.update(kwargs)
    return WorkflowState(**defaults)


def _authorizer(mode: str = "enforce", declared=_DECLARED) -> Authorizer:
    return Authorizer(declared_tables=lambda _ds: declared, mode=mode)


def _node(spy: _SpyConnectors, **kwargs):
    return make_execute_sql(spy, **kwargs)  # budget 未装配 = 不判成本(本文件考授权)


def _user(grants=frozenset({DEFAULT})) -> dict:
    return principal_to_wire(Principal(subject="7", grants=grants))


# ── A1:没有主体就不执行(I2 / I7)────────────────────────────────


class TestNoPrincipal:
    async def test_missing_principal_is_refused(self):
        spy = _SpyConnectors()
        update = await _node(spy, authorizer=_authorizer())(_state())
        assert "AUTHZ_NO_PRINCIPAL" in update["error"]
        assert spy.executed == [], "拒绝的 SQL 不得被执行"

    async def test_default_workflow_state_carries_no_principal(self):
        """回归门:``WorkflowState`` 的默认值必须是 fail-closed。

        只要有一天有人图省事把默认值改成「本地 admin」,这条会红。
        """
        assert WorkflowState(session_id="s", question="q").principal is None

    async def test_refusal_is_terminal_not_a_correction_round(self):
        """授权拒绝**不可修正** —— 不喂回 gen_sql 重生成。

        与只读门同理(安全违规不是生成缺陷):让模型重写十遍也改不掉「这个
        用户没有这张表的权限」,只会烧掉共享修正预算。
        """
        spy = _SpyConnectors()
        update = await _node(spy, authorizer=_authorizer())(_state())
        assert "error" in update
        assert "error_feedback" not in update
        assert "retry_count" not in update

    async def test_nothing_is_cleared_on_refusal(self):
        """拒绝时不得顺手清结果集 —— 那会把上一轮的成功产物抹掉。"""
        spy = _SpyConnectors()
        update = await _node(spy, authorizer=_authorizer())(_state())
        assert "rows" not in update and "columns" not in update


# ── A2:数据源授权 ────────────────────────────────────────────────


class TestDatasourceGate:
    async def test_datasource_outside_grants_is_refused(self):
        spy = _SpyConnectors()
        node = _node(spy, authorizer=_authorizer())
        update = await node(_state(principal=_user(frozenset({"other_db"}))))
        assert "AUTHZ_DATASOURCE" in update["error"]
        assert spy.executed == []

    async def test_granted_datasource_executes(self):
        spy = _SpyConnectors()
        update = await _node(spy, authorizer=_authorizer())(_state(principal=_user()))
        assert update["row_count"] == 1
        assert spy.executed == ["SELECT count(*) FROM students"]

    async def test_admin_executes(self):
        spy = _SpyConnectors()
        update = await _node(spy, authorizer=_authorizer())(
            _state(principal=principal_to_wire(Principal(subject="1", role="admin")))
        )
        assert update["row_count"] == 1


# ── A3:表级(语义层白名单)────────────────────────────────────────


class TestTableGate:
    async def test_undeclared_table_is_refused_in_enforce(self):
        spy = _SpyConnectors()
        node = _node(spy, authorizer=_authorizer(mode="enforce"))
        update = await node(_state(sql="SELECT * FROM salaries", principal=_user()))
        assert "AUTHZ_TABLE" in update["error"]
        assert "salaries" in update["error"]
        assert spy.executed == [], "表级拒绝同样不得执行"

    async def test_undeclared_table_still_executes_in_warn(self):
        """warn 期(§8.2)放行 + 记录 —— 观察期要能收集「哪些表会被拒」。"""
        spy = _SpyConnectors()
        node = _node(spy, authorizer=_authorizer(mode="warn"))
        update = await node(_state(sql="SELECT * FROM salaries", principal=_user()))
        assert update["row_count"] == 1
        assert spy.executed == ["SELECT * FROM salaries"]

    async def test_decision_snapshot_is_written_to_state(self):
        """判定写进 state 供审计与可观测(§6.2)。"""
        spy = _SpyConnectors()
        node = _node(spy, authorizer=_authorizer(mode="warn"))
        update = await node(_state(sql="SELECT * FROM salaries", principal=_user()))
        assert update["authz_decision"]["narrowed_tables"] == ["salaries"]

    async def test_enforce_denial_records_the_snapshot_too(self):
        spy = _SpyConnectors()
        node = _node(spy, authorizer=_authorizer(mode="enforce"))
        update = await node(_state(sql="SELECT * FROM salaries", principal=_user()))
        assert update["authz_decision"]["reason"] == "table"


class TestDenyMetric:
    """拒绝计数记在**节点**上,不记在 ``Authorizer`` 里(设计 §9.2 / P5)。

    同一个 ``check`` 在「这条 SQL 要不要落库」的判定里跑一次,记在这里就等于
    记「这次执行被闸门拦了」;记在服务层则会把所有**只判定不执行**的调用
    (探针、预检、将来的「先问一句能不能查」)一起算进拒绝率 —— 那些调用没有
    落库,运维拿这个数报警会打到空处。与 ``trove_sql_budget_decisions_total``
    只在执行节点记是同一条纪律。
    """

    async def test_a_table_denial_is_counted(self):
        from trove.core.metrics import render_metrics

        node = _node(_SpyConnectors(), authorizer=_authorizer(mode="enforce"))
        await node(_state(sql="SELECT * FROM salaries", principal=_user()))

        text = render_metrics().decode()
        assert 'trove_authz_deny_total{reason="table"}' in text

    async def test_a_warn_pass_is_not_a_denial(self):
        """warn 期放行的 A3 判定**不**进拒绝计数 —— 它没有被拦,只是被记了。"""
        from trove.core.metrics import render_metrics

        before = render_metrics().decode().count('reason="table"')
        node = _node(_SpyConnectors(), authorizer=_authorizer(mode="warn"))
        await node(_state(sql="SELECT * FROM salaries", principal=_user()))
        after = render_metrics().decode().count('reason="table"')

        assert after == before

    async def test_a_warn_pass_is_counted_on_its_own_series(self):
        """warn 命中有**自己**的计数器 —— §8.2 观察期要的是量,日志给不了。

        一行一条的日志答不了「切 enforce 会打挂多少」。这条计数器是那个分母,
        而它与拒绝计数**分家**:并进去,运维读到的告警率里会混进一半没被拦的查询。
        """
        before = _warn_total()
        node = _node(_SpyConnectors(), authorizer=_authorizer(mode="warn"))
        await node(_state(sql="SELECT * FROM salaries", principal=_user()))

        assert _warn_total() == before + 1

    async def test_an_enforced_denial_does_not_count_as_a_warn_pass(self):
        """enforce 期被拦下的那一次**不**记 warn —— 它根本没被放行。

        两条口径混了,「warn 期会放行多少」这个数就会把已经拦掉的那些也算进去,
        切档的决策会偏保守到不敢切。
        """
        before = _warn_total()
        node = _node(_SpyConnectors(), authorizer=_authorizer(mode="enforce"))
        await node(_state(sql="SELECT * FROM salaries", principal=_user()))

        assert _warn_total() == before

    async def test_a_clean_pass_does_not_count_as_a_warn_pass(self):
        """没命中未声明表就不记 —— 「放行了且没越界」不是观察期的信号。

        ``students`` 在 ``_DECLARED`` 里,所以这次既没拒也没记。
        """
        before = _warn_total()
        node = _node(_SpyConnectors(), authorizer=_authorizer(mode="warn"))
        await node(_state(sql="SELECT count(*) FROM students", principal=_user()))

        assert _warn_total() == before


# ── 未装配与形状 ─────────────────────────────────────────────────


class TestWiring:
    async def test_no_authorizer_means_no_enforcement(self):
        """未装配强制点 = 不判(不是「判过了、放行」)。

        生产图总是装配一个(见 ``test_graphs`` 的同一断言);这条说明的是
        ``make_execute_sql`` 单独使用时**不假装自己判过**。
        """
        spy = _SpyConnectors()
        update = await _node(spy)(_state())
        assert update["row_count"] == 1

    async def test_principal_is_read_back_from_the_wire_form(self):
        """节点读的是 state 里的 wire 字典,不是 dataclass。"""
        spy = _SpyConnectors()
        node = _node(spy, authorizer=_authorizer())
        update = await node(
            _state(
                sql="SELECT * FROM salaries",
                principal=principal_to_wire(Principal(subject="7", grants=frozenset({DEFAULT}))),
            )
        )
        assert "AUTHZ_TABLE" in update["error"]

    async def test_malformed_principal_wire_is_refused(self):
        """wire 形状坏掉 → 当作没有主体 → 拒绝,而不是猜一个主体出来。"""
        spy = _SpyConnectors()
        node = _node(spy, authorizer=_authorizer())
        update = await node(_state(principal={"subject": "7", "grants": {"a": 1}}))
        assert "AUTHZ_NO_PRINCIPAL" in update["error"]
        assert spy.executed == []

    async def test_upstream_error_still_wins(self):
        """上游已失败 → 原样透传,不额外报一个授权错。"""
        spy = _SpyConnectors()
        node = _node(spy, authorizer=_authorizer())
        assert await node(_state(error="upstream boom")) == {}

    @pytest.mark.parametrize("mode", ["warn", "enforce"])
    async def test_mode_does_not_change_a1_or_a2(self, mode):
        """warn 只放宽 A3。放宽 A1/A2 会让 warn 变成「关掉鉴权」。"""
        spy = _SpyConnectors()
        node = _node(spy, authorizer=_authorizer(mode=mode))
        update = await node(_state(principal=None))
        assert "AUTHZ_NO_PRINCIPAL" in update["error"]
        update = await node(_state(principal=_user(frozenset({"other"}))))
        assert "AUTHZ_DATASOURCE" in update["error"]
        assert spy.executed == []


# ── 行/列 scope(设计 §2.3)─────────────────────────────────────


def _scoped_user(*, row=None, deny=None, grants=frozenset({DEFAULT})) -> dict:
    return principal_to_wire(Principal(
        subject="7", grants=grants,
        row_grants=None if row is None else {DEFAULT: row},
        column_denials=(
            None if deny is None
            else {DEFAULT: {t: frozenset(c) for t, c in deny.items()}}
        ),
    ))


class TestRowColumnScope:
    """执行前的行/列改写与拒绝 —— 同一份 principal 既判 A1–A3 也判行/列。

    与 A1–A3 相同的两件事:**拒绝的 SQL 不得执行**,且拒绝是**终态**
    (不可修正,不喂回生成方)。
    """

    async def test_row_grant_rewrites_the_executed_sql(self):
        spy = _SpyConnectors()
        update = await _node(spy, authorizer=_authorizer())(
            _state(principal=_scoped_user(row={"students": "class = 'A'"})))
        assert update["row_count"] == 1
        expected = ("SELECT COUNT(*) FROM (SELECT * FROM students "
                    "WHERE (students.class = 'A')) AS students")
        assert spy.executed == [expected]
        # 用户看到的 SQL 与授权后的执行面一致
        assert update["sql"] == expected
        assert update["data_scope"] == {
            "row_filters": ["students"], "rewritten": True,
            "column_policy": False,
        }

    async def test_no_grants_is_byte_identical(self):
        """未声明 = 不解析、不改写 —— 存量部署的执行链路上不多一次往返。"""
        spy = _SpyConnectors()
        update = await _node(spy, authorizer=_authorizer())(_state(principal=_user()))
        assert spy.executed == ["SELECT count(*) FROM students"]
        assert update["sql"] == "SELECT count(*) FROM students"
        assert update["data_scope"] == {
            "row_filters": [], "rewritten": False, "column_policy": False,
        }

    async def test_blocked_column_is_refused_before_execution(self):
        spy = _SpyConnectors()
        update = await _node(spy, authorizer=_authorizer())(
            _state(sql="SELECT id, email FROM students",
                   principal=_scoped_user(deny={"students": ["email"]})))
        assert "[ERR:AUTHZ_COLUMN]" in update["error"]
        assert "email" in update["error"]
        assert spy.executed == [], "被禁列拒绝的 SQL 不得执行"
        assert "error_feedback" not in update, "列权限不可修正,不进重试环"
        assert update["authz_decision"]["reason"] == "column"
        assert update["authz_decision"]["allowed"] is False

    async def test_star_projection_is_refused_for_tables_with_denials(self):
        spy = _SpyConnectors()
        update = await _node(spy, authorizer=_authorizer())(
            _state(sql="SELECT * FROM students",
                   principal=_scoped_user(deny={"students": ["email"]})))
        assert "[ERR:AUTHZ_COLUMN]" in update["error"]
        assert spy.executed == []

    async def test_count_star_still_passes(self):
        """``COUNT(*)`` 不带出任何值 —— 星号只在直接投影位置才是拒绝面。"""
        spy = _SpyConnectors()
        update = await _node(spy, authorizer=_authorizer())(
            _state(principal=_scoped_user(deny={"students": ["email"]})))
        assert update["row_count"] == 1
        assert update["data_scope"]["column_policy"] is True

    async def test_unparseable_sql_with_grants_is_refused(self):
        spy = _SpyConnectors()
        update = await _node(spy, authorizer=_authorizer())(
            _state(sql="SELECT FROM WHERE ((",
                   principal=_scoped_user(row={"students": "class = 'A'"})))
        assert "[ERR:AUTHZ_UNRESOLVED]" in update["error"]
        assert spy.executed == []

    async def test_scope_applies_even_when_authorizer_is_not_wired(self):
        """少装一个组件不能放宽:主体带着 grant 就必须生效(同
        ``authorize_and_scope`` 的纪律)。"""
        spy = _SpyConnectors()
        update = await _node(spy)(
            _state(principal=_scoped_user(row={"students": "id > 5"})))
        assert update["row_count"] == 1
        assert "WHERE (students.id > 5)" in spy.executed[0]

    async def test_no_principal_without_authorizer_still_executes(self):
        """未装配强制点 + 无主体 = 不判(嵌入部署,存量行为不变)。"""
        spy = _SpyConnectors()
        update = await _node(spy)(_state(principal=None))
        assert update["row_count"] == 1
        assert update["data_scope"] is None

    async def test_column_denial_is_counted_on_its_own_reason(self):
        from trove.core.metrics import render_metrics

        node = _node(_SpyConnectors(), authorizer=_authorizer())
        await node(_state(sql="SELECT email FROM students",
                          principal=_scoped_user(deny={"students": ["email"]})))
        assert 'trove_authz_deny_total{reason="column"}' in render_metrics().decode()


class _Layer:
    """语义层替身:``model()`` 返回一份声明了 row_filter 的模型。"""

    def __init__(self, model):
        self._model = model

    def model(self):
        if isinstance(self._model, Exception):
            raise self._model
        return self._model


class TestDeclaredRLSOnGenerationPath:
    MODEL = SemanticModel(datasets=[
        SemanticDataset(name="students", source="students",
                        row_filter="class = 'A'"),
    ])

    async def test_generation_path_injects_declared_rls(self):
        """生成路径此前**从未**注入过声明 RLS(无契约、非快径)——
        本次由 scope 补上。"""
        spy = _SpyConnectors()
        update = await _node(spy, authorizer=_authorizer(), semantic_layer=_Layer(self.MODEL))(
            _state(principal=_user()))
        assert spy.executed == [
            "SELECT COUNT(*) FROM (SELECT * FROM students "
            "WHERE (students.class = 'A')) AS students"]
        assert update["data_scope"]["row_filters"] == ["students"]

    async def test_compiled_path_does_not_inject_a_second_copy(self):
        """快径/编译路径已在 SQL 里注入(这里以 ``fast_path`` 声明)→
        不再补一份。"""
        spy = _SpyConnectors()
        update = await _node(spy, authorizer=_authorizer(), semantic_layer=_Layer(self.MODEL))(
            _state(principal=_user(), fast_path=True))
        assert spy.executed == ["SELECT count(*) FROM students"]
        assert update["data_scope"]["rewritten"] is False

    async def test_model_unreadable_falls_back_to_principal_grants(self):
        """语义层故障 → 不补声明 RLS(回到改造前的覆盖面),但主体 grant 照常
        生效 —— 故障不能变成放宽。"""
        spy = _SpyConnectors()
        update = await _node(
            spy, authorizer=_authorizer(),
            semantic_layer=_Layer(RuntimeError("boom")),
        )(_state(principal=_scoped_user(row={"students": "id > 5"})))
        assert update["row_count"] == 1
        assert "WHERE (students.id > 5)" in spy.executed[0]
        assert "class = 'A'" not in spy.executed[0]


class _SpyBudget:
    """记录估算收到的 SQL;``verdict="degrade"`` 时验证 LIMIT 叠在 scope 之后。"""

    def __init__(self, verdict: str = "allow") -> None:
        from trove.services.sql.budget import ExecutionBudget

        self.verdict = verdict
        self.budget = ExecutionBudget()
        self.estimated: list[str] = []

    async def estimate(self, datasource, sql, dialect, tables):
        from trove.services.sql.budget import CostEstimate

        self.estimated.append(sql)
        return CostEstimate(10, None, "explain", False, {})

    def decide(self, est):
        from trove.services.sql.budget import BudgetDecision

        return BudgetDecision(self.verdict)


class TestBudgetSeesScopedSQL:
    async def test_estimate_gets_the_scoped_sql(self):
        spy, budget = _SpyConnectors(), _SpyBudget()
        await _node(spy, authorizer=_authorizer(), budget=budget)(
            _state(principal=_scoped_user(row={"students": "id > 5"})))
        assert budget.estimated == spy.executed
        assert "WHERE (students.id > 5)" in budget.estimated[0]

    async def test_degrade_limit_is_applied_on_top_of_the_scoped_sql(self):
        """降级加 LIMIT 必须作用在 scope 之后 —— 对原串加 LIMIT 会把行过滤
        整段丢掉(安全洞,不只是账不对)。"""
        spy = _SpyConnectors()
        await _node(spy, authorizer=_authorizer(), budget=_SpyBudget("degrade"))(
            _state(principal=_scoped_user(row={"students": "id > 5"})))
        executed = spy.executed[0]
        assert "WHERE (students.id > 5)" in executed
        assert "LIMIT" in executed.upper()
