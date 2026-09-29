"""授权策略单测:三种 grants 取值、scope 规则、narrow 只收窄、单一实现。

对应设计文档 ``2026-09-28-agent-identity-masking-design.md`` 的
I1 / I6 / I7 与验收项 A1。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

import trove
from trove.services.authz.policy import (
    Policy,
    Principal,
    principal_from_wire,
    principal_to_wire,
    scopes_allow,
    visible_datasources,
)
from trove.services.semantic_layer.models import MaskingPolicy

TROVE_ROOT = Path(trove.__file__).parent


class _FakeAuth:
    """最小 AuthService 替身,记录被查过哪些 user id。"""

    def __init__(self, grants_by_user: dict | None = None, boom: bool = False):
        self._grants = grants_by_user or {}
        self._boom = boom
        self.asked: list = []

    async def get_datasources(self, user_id):
        self.asked.append(user_id)
        if self._boom:
            raise RuntimeError("app.db is unreachable")
        return list(self._grants.get(user_id, []))


# ── grants 三种取值(I7 的核心)────────────────────────────────────


class TestAllowsDatasource:
    def test_default_principal_denies_everything(self):
        """字段默认值必须是 fail-closed —— 忘记传 grants 不能变成全放行。"""
        p = Principal(subject="1")
        assert p.allows_datasource("any", "any") is False

    def test_admin_bypasses_grants(self):
        p = Principal(subject="1", role="admin", grants=None)
        assert p.allows_datasource("any", "any") is True

    def test_admin_bypasses_even_with_narrow_grants(self):
        """admin 判定在读 grants **之前** —— 沿用存量语义(admin 全放行)。"""
        p = Principal(subject="1", role="admin", grants=frozenset({"sales"}))
        assert p.allows_datasource("financial", "financial") is True

    def test_empty_grants_allow_default_only(self):
        p = Principal(subject="1", grants=frozenset())
        assert p.allows_datasource("test_db", "test_db") is True
        assert p.allows_datasource("other", "test_db") is False

    def test_empty_grants_without_default_denies(self):
        """注册表没有默认源时,空 grants 连默认源都放行不了。"""
        p = Principal(subject="1", grants=frozenset())
        assert p.allows_datasource("test_db", None) is False

    def test_nonempty_grants_are_strict_allowlist(self):
        p = Principal(subject="1", grants=frozenset({"sales"}))
        assert p.allows_datasource("sales", "financial") is True
        # 在 grants 里就是放行 —— 即使它不是默认源
        assert p.allows_datasource("other", "financial") is False

    def test_no_evidence_denies_even_the_default(self):
        """**这条是本次语义变更的分界。**

        ``grants is None``(拿不到授权依据)曾被当成"空 grants"处理,于是放行默认源。
        若该用户真实 grants 是 ``{sales}``、默认源是 ``financial``,这就把他提权了。
        空集是"有依据且依据为空",None 是"没有依据",两者必须分开。
        """
        p = Principal(subject="1", grants=None)
        assert p.allows_datasource("financial", "financial") is False


# ── scope 规则(「空 = 不限」只有一份)──────────────────────────────


class TestScopesAllow:
    def test_undeclared_scopes_are_unrestricted(self):
        assert scopes_allow(None, "admin") is True
        assert scopes_allow([], "admin") is True

    def test_declared_scopes_must_intersect(self):
        assert scopes_allow(["admin"], "admin") is True
        assert scopes_allow(["query"], "admin") is False

    def test_single_required_is_membership(self):
        """``require_admin`` 原来用 ``in``,与 ``&`` 在多元素时才是两个语义。"""
        assert scopes_allow(["query", "admin"], "admin") is True
        assert scopes_allow(["query"], "admin") is False

    def test_multiple_required_needs_one_hit(self):
        assert scopes_allow(["query"], "query", "admin") is True
        assert scopes_allow(["export"], "query", "admin") is False

    def test_no_requirement_never_blocks(self):
        assert scopes_allow(["query"]) is True

    def test_principal_delegates(self):
        p = Principal(subject="1", scopes=frozenset({"query"}))
        assert p.has_scope("query") is True
        assert p.has_scope("admin") is False


# ── PII bypass:与「空 = 不限」**相反**的一条门(设计 §5.5 / §5.6)────


class TestMayBypassMasking:
    """``may_bypass_masking`` —— 持 ``bypass_scopes`` 之一或 ``default_policy ==
    "bypass"`` 才看原文。

    这条门与本文件上面那条规则**刻意相反**:``scopes_allow`` 的「空 scopes = 不限」
    是给路由门的存量兼容(方向宽);这里空 scopes 必须意味着「没有 pii」——
    否则每个存量 token、以及 §5.6 的 ``on_behalf_of`` 重放,都直接看到原文。
    下面 ``test_empty_scopes_are_not_unrestricted`` / ``test_decision_never_goes_
    through_scopes_allow`` 两条就是这条方向的守卫。
    """

    @staticmethod
    def _policy(**overrides):
        return MaskingPolicy(**overrides)

    def test_holding_a_bypass_scope_bypasses(self):
        p = Principal(subject="7", scopes=frozenset({"pii"}))
        assert Policy.may_bypass_masking(p, self._policy(bypass_scopes=["pii"])) is True

    def test_any_one_of_the_declared_scopes_suffices(self):
        p = Principal(subject="7", scopes=frozenset({"gdpr"}))
        policy = self._policy(bypass_scopes=["pii", "gdpr"])
        assert Policy.may_bypass_masking(p, policy) is True

    def test_other_scopes_do_not_bypass(self):
        p = Principal(subject="7", scopes=frozenset({"query", "export"}))
        assert Policy.may_bypass_masking(p, self._policy(bypass_scopes=["pii"])) is False

    def test_empty_scopes_are_not_unrestricted(self):
        """**本类最重要的一条。** 空 scopes 走 ``scopes_allow`` 会被读成"不限" ——
        那是存量 token 全量看原文,也是 §5.6 重放通道的洞。"""
        p = Principal(subject="7", scopes=frozenset())
        assert Policy.may_bypass_masking(p, self._policy(bypass_scopes=["pii"])) is False
        assert scopes_allow(p.scopes, "pii") is True, "前提没变:scopes_allow 仍是宽的那条"

    def test_admin_does_not_bypass_without_the_scope(self):
        """§8.4:admin 是运维角色,不是数据授权角色。"""
        p = Principal(subject="1", role="admin")
        assert Policy.may_bypass_masking(p, self._policy(bypass_scopes=["pii"])) is False

    def test_replayed_principal_is_judged_by_its_own_scopes(self):
        """§5.6:重放按目标用户的权限走 —— ``on_behalf_of`` 不构成豁免。"""
        replayed = Principal(subject="42", scopes=frozenset(), on_behalf_of="7")
        assert Policy.may_bypass_masking(replayed, self._policy(bypass_scopes=["pii"])) is False
        allowed = Principal(subject="42", scopes=frozenset({"pii"}), on_behalf_of="7")
        assert Policy.may_bypass_masking(allowed, self._policy(bypass_scopes=["pii"])) is True

    def test_default_policy_bypass_applies_to_everyone(self):
        p = Principal(subject="7", scopes=frozenset())
        assert Policy.may_bypass_masking(p, self._policy(default_policy="bypass")) is True

    @pytest.mark.parametrize("declared", ["apply", "", "BY PASS", "warn"])
    def test_only_the_exact_bypass_value_bypasses(self, declared):
        """认不出的取值一律按 apply(判据缺失的方向是严),不做大小写/近义猜测。"""
        p = Principal(subject="7", scopes=frozenset())
        assert Policy.may_bypass_masking(p, self._policy(default_policy=declared)) is False

    def test_bypass_scopes_are_not_built_in(self):
        """``pii`` 只在模型声明了它的时候才算数 —— 口令不是内建的。"""
        p = Principal(subject="7", scopes=frozenset({"pii"}))
        assert Policy.may_bypass_masking(p, self._policy()) is False

    def test_blank_declared_scope_never_matches(self):
        """声明表里的空串不能被当成"任意 scope"的通行符。"""
        p = Principal(subject="7", scopes=frozenset({"", "query"}))
        assert Policy.may_bypass_masking(p, self._policy(bypass_scopes=[""])) is False

    def test_missing_policy_is_fail_closed(self):
        p = Principal(subject="7", scopes=frozenset({"pii"}))
        assert Policy.may_bypass_masking(p, None) is False

    def test_scalar_bypass_scopes_declaration_grants_nothing(self):
        """声明写成了标量(``bypass_scopes: pii``)时不能按字符拆成 ``{p,i}`` ——
        那既不放行 ``pii``,又会在有人真持 ``p`` 时意外放行。按"没声明"处理。"""
        class _Scalar:
            bypass_scopes = "pii"
            default_policy = "apply"

        assert Policy.may_bypass_masking(Principal(subject="7", scopes=frozenset({"pii"})),
                                         _Scalar()) is False
        assert Policy.may_bypass_masking(Principal(subject="8", scopes=frozenset({"p"})),
                                         _Scalar()) is False

    def test_decision_never_goes_through_scopes_allow(self, monkeypatch):
        """**不许走 ``scopes_allow`` / ``has_scope``**(任务书的硬约束)。

        把这两条路径改成抛异常:判定仍然要得出正确结论 —— 一旦谁"顺手"改成
        委托 ``has_scope``,空 scopes 立刻变回"不限",这条测试就是那道闸。
        """
        from trove.services.authz import policy as policy_module

        def boom(*args, **kwargs):  # pragma: no cover - 被调用即失败
            raise AssertionError("may_bypass_masking 不得走 scopes_allow/has_scope")

        monkeypatch.setattr(policy_module, "scopes_allow", boom)
        monkeypatch.setattr(Principal, "has_scope", boom)
        holder = Principal(subject="7", scopes=frozenset({"pii"}))
        empty = Principal(subject="8", scopes=frozenset())

        assert Policy.may_bypass_masking(holder, self._policy(bypass_scopes=["pii"])) is True
        assert Policy.may_bypass_masking(empty, self._policy(bypass_scopes=["pii"])) is False

    def test_usable_from_class_and_instance(self):
        """设计 §7.1 写的是实例方法,实现与 ``has_scope`` 同款走 staticmethod ——
        两种调法都要能用(调用点不必先构造 Policy)。"""
        p = Principal(subject="7", scopes=frozenset({"pii"}))
        policy = self._policy(bypass_scopes=["pii"])
        assert Policy().may_bypass_masking(p, policy) is True
        assert Policy.may_bypass_masking(p, policy) is True



# ── 构造主体(全仓唯一的 get_datasources 调用点)────────────────────


class TestPrincipalFor:
    async def test_admin_does_not_read_grants(self):
        auth = _FakeAuth({7: ["sales"]})
        p = await Policy(auth).principal_for({"id": 7, "role": "admin"})
        assert p.grants is None
        assert auth.asked == []          # admin 判定在读 grants 之前

    async def test_user_grants_are_fetched_and_frozen(self):
        auth = _FakeAuth({7: ["sales", "ops"]})
        p = await Policy(auth).principal_for({"id": 7, "role": "user"})
        assert p.grants == frozenset({"sales", "ops"})
        assert p.subject == "7"

    async def test_missing_auth_service_yields_no_evidence(self):
        """没有 auth 服务 = 查不到依据,而不是"依据为空"。"""
        p = await Policy(None).principal_for({"id": 7, "role": "user"})
        assert p.grants is None
        assert p.allows_datasource("test_db", "test_db") is False

    async def test_store_failure_propagates(self):
        """存储故障往上抛(HTTP 500),不被翻译成任何一个授权结论。

        翻译成 403 会让排障方向整个错掉(看起来像权限问题);翻译成放行则是提权。
        """
        with pytest.raises(RuntimeError, match="unreachable"):
            await Policy(_FakeAuth(boom=True)).principal_for({"id": 7, "role": "user"})

    async def test_scopes_carried_over(self):
        p = await Policy(_FakeAuth()).principal_for(
            {"id": 7, "role": "user", "scopes": ["pii"]},
        )
        assert p.has_scope("pii") is True
        assert p.has_scope("admin") is False


# ── I6:只能收窄 ──────────────────────────────────────────────────


class TestNarrow:
    def test_scopes_intersect(self):
        p = Principal(subject="1", scopes=frozenset({"query", "pii"}))
        assert p.narrow(scopes=["query"]).scopes == frozenset({"query"})

    def test_scopes_cannot_widen(self):
        p = Principal(subject="1", scopes=frozenset({"query"}))
        assert p.narrow(scopes=["query", "admin"]).scopes == frozenset({"query"})

    def test_grants_intersect(self):
        p = Principal(subject="1", grants=frozenset({"sales", "ops"}))
        assert p.narrow(grants=["sales"]).grants == frozenset({"sales"})

    def test_grants_cannot_widen(self):
        p = Principal(subject="1", grants=frozenset({"sales"}))
        assert p.narrow(grants=["sales", "financial"]).grants == frozenset({"sales"})

    def test_admin_narrowed_to_user_carries_the_given_grants(self):
        """admin 原本不受限,收窄时对方给的集合就是上界(交集 = 它本身)。"""
        p = Principal(subject="1", role="admin")
        narrowed = p.narrow(role="user", grants=["sales"])
        assert narrowed.role == "user"
        assert narrowed.grants == frozenset({"sales"})
        assert narrowed.allows_datasource("financial", "financial") is False

    def test_role_cannot_be_raised(self):
        p = Principal(subject="1", role="user")
        with pytest.raises(ValueError, match="不能把 role"):
            p.narrow(role="admin")

    def test_unknown_field_rejected(self):
        with pytest.raises(ValueError, match="不认识"):
            Principal(subject="1").narrow(grantsz=["sales"])

    def test_original_is_untouched(self):
        p = Principal(subject="1", grants=frozenset({"sales", "ops"}))
        p.narrow(grants=["sales"])
        assert p.grants == frozenset({"sales", "ops"})


# ── 可见性列表 ───────────────────────────────────────────────────


class TestVisibleDatasources:
    def test_admin_sees_all_in_order(self):
        p = Principal(subject="1", role="admin")
        assert visible_datasources(p, ["a", "b", "c"], "a") == ["a", "b", "c"]

    def test_user_sees_allowlist_intersection(self):
        p = Principal(subject="1", grants=frozenset({"a", "c"}))
        assert visible_datasources(p, ["a", "b", "c"], "a") == ["a", "c"]

    def test_empty_grants_sees_default_only(self):
        p = Principal(subject="1", grants=frozenset())
        assert visible_datasources(p, ["a", "b"], "b") == ["b"]

    def test_no_evidence_sees_nothing(self):
        p = Principal(subject="1", grants=None)
        assert visible_datasources(p, ["a", "b"], "a") == []


# ── wire 形状:主体要跨 checkpointer ─────────────────────────────


class TestPrincipalWire:
    """``Principal`` 跨节点边界的形状(设计 §6.2 的落地修正)。

    §6.2 原写 ``principal: Principal | None`` 直接进 ``WorkflowState``。实测
    这是**踩了 ``contract.py`` 记录过的同一个坑**:LangGraph 每个超级步都拿
    state 过一次序列化,``JsonPlusSerializer`` 对未注册的 dataclass 在非严格
    模式下降级、在 ``LANGGRAPH_STRICT_MSGPACK=true`` 下**直接拦掉**。降级的
    形态是**普通 dict** —— 而安全判定点恰好调 ``.allows_datasource()``,拿到
    dict 就是 ``AttributeError``。安全判定点不该拿到意料之外的形状。

    所以主体按 ``contract_to_wire`` / ``contract_from_wire`` 的既有先例走
    纯 JSON 形状。**三种 grants 取值必须原样活过往返** —— 把 ``frozenset()``
    塌成 ``None`` 就是把「依据为空」读成「没有依据」,方向恰好相反。
    """

    def test_none_principal_stays_none(self):
        assert principal_to_wire(None) is None
        assert principal_from_wire(None) is None

    @pytest.mark.parametrize(
        "grants",
        [None, frozenset(), frozenset({"a", "b"})],
        ids=["none", "empty", "nonempty"],
    )
    def test_round_trip_preserves_the_three_grants_states(self, grants):
        """三种取值是三个语义,往返不得塌陷成两种。"""
        p = Principal(
            subject="7", role="user", scopes=frozenset({"pii"}),
            grants=grants, on_behalf_of="42",
        )
        back = principal_from_wire(principal_to_wire(p))
        assert back == p
        assert (back.grants is None) is (grants is None)

    def test_admin_round_trips(self):
        p = Principal(subject="1", role="admin")
        assert principal_from_wire(principal_to_wire(p)) == p

    def test_wire_is_json_safe(self):
        """wire 里不得留下 frozenset / tuple —— 那正是跨不过边界的形状。"""
        wire = principal_to_wire(
            Principal(subject="1", scopes=frozenset({"a"}), grants=frozenset({"b"}))
        )
        json.dumps(wire)  # 不抛 = 纯 JSON
        assert isinstance(wire["scopes"], list)
        assert isinstance(wire["grants"], list)

    def test_wire_never_serializes_the_dataclass_itself(self):
        """回归门:别再把 dataclass 塞进 state。"""
        wire = principal_to_wire(Principal(subject="1"))
        assert type(wire) is dict
        assert not isinstance(wire, Principal)

    def test_malformed_wire_is_no_principal_not_a_guess(self):
        """形状异常 → ``None``(= 没有主体 → 拒绝),不尽量解出一部分。

        「尽量解出」在这里是**放宽**:缺 grants 的半个主体会被读成
        ``grants=None``(拒绝一切,方向对)或漏掉 role(丢掉 admin),
        两种都不是调用方想要的东西。``None`` 是明确的「没有主体」信号。
        """
        for bad in (
            {"subject": "1", "role": 3},          # role 不是字符串
            {"role": "user"},                      # 缺 subject
            "not-a-dict",
            ["subject"],
            {"subject": "1", "scopes": "pii"},     # scopes 不是列表
            {"subject": "1", "grants": {"a": 1}},  # grants 不是列表
        ):
            assert principal_from_wire(bad) is None, bad

    def test_unknown_keys_are_ignored(self):
        """向前兼容:多出来的键不该让整个主体作废(旧代码读新 checkpoint)。"""
        p = principal_from_wire({"subject": "1", "role": "user", "future": 1})
        assert p is not None and p.subject == "1"

    def test_survives_the_real_checkpointer_serializer(self):
        """真 serde 往返 —— 这才是「跨节点边界」的实际执行者。

        关键断言是**回来还是同一形态**:不是 dataclass、也不是被降级的
        dict,而是能原样 ``principal_from_wire`` 回主体的那份 wire。
        """
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

        serde = JsonPlusSerializer()
        for grants in (None, frozenset(), frozenset({"a"})):
            p = Principal(subject="7", scopes=frozenset({"pii"}), grants=grants)
            wire = principal_to_wire(p)
            back = serde.loads_typed(serde.dumps_typed(wire))
            assert type(back) is dict, f"被降级成了 {type(back)}"
            assert principal_from_wire(back) == p


# ── I1:策略只有一个实现 ───────────────────────────────────────────


#: 允许出现 ``.get_datasources(`` **调用**的文件(相对 ``trove/``)。
#: 定义在 ``services/auth/store.py`` / ``services/auth/service.py``;
#: 管理端点读写 grants 本身,本就不该走策略层,故豁免。
_ALLOWED_GRANTS_CALLERS = {
    "services/authz/policy.py",
    "api/routers/admin.py",
    "cli/admin_cmds.py",
}


def _get_datasources_call_sites() -> set[str]:
    """扫出所有 ``x.get_datasources(...)`` 调用所在的文件。

    用 AST 而不是 grep:注释与文档字符串里提到这个名字很常见(本次改动就写了
    好几处),grep 会把它们误判成调用点,于是白名单被迫放宽到失去意义。
    """
    sites: set[str] = set()
    for path in TROVE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get_datasources"
            ):
                sites.add(path.relative_to(TROVE_ROOT).as_posix())
    return sites


class TestSingleImplementation:
    """I1 的执行机制(设计 §9.3)。

    收敛的不只是「判定」,还有**取数权**:四处各判各的还能靠纪律对齐,四处各
    **取**各的则连比较的基准都不存在 —— 一致性测试没有意义,因为两边读的可能
    根本不是同一份 grants。
    """

    def test_grants_are_fetched_only_where_allowed(self):
        sites = _get_datasources_call_sites()
        unexpected = sites - _ALLOWED_GRANTS_CALLERS
        assert not unexpected, (
            f"这些文件在策略层之外取 grants: {sorted(unexpected)}\n"
            f"  数据源授权判定必须走 services/authz/policy.py —— "
            f"四份手抄的副本已经漂移过一次(设计 §2.2 G1)"
        )

    def test_policy_is_actually_a_caller(self):
        """白名单不能因为改名而空转:policy.py 必须真的在里面。"""
        assert "services/authz/policy.py" in _get_datasources_call_sites()

    def test_rewired_entries_are_gone(self):
        """本次转调掉的三个调用点不得复活。"""
        sites = _get_datasources_call_sites()
        for gone in ("api/deps.py", "mcp/server.py", "api/routers/catalog.py"):
            assert gone not in sites, f"{gone} 又自己取 grants 了"
