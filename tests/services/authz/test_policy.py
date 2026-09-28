"""授权策略单测:三种 grants 取值、scope 规则、narrow 只收窄、单一实现。

对应设计文档 ``2026-09-28-agent-identity-masking-design.md`` 的
I1 / I6 / I7 与验收项 A1。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import trove
from trove.services.authz.policy import (
    Policy,
    Principal,
    scopes_allow,
    visible_datasources,
)

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
