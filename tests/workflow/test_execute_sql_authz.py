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
from trove.workflow.nodes.execute_sql import make_execute_sql
from trove.workflow.state import WorkflowState

DEFAULT = "test_db"

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
    return make_execute_sql(spy, explain_row_guard=False, **kwargs)


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
