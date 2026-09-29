"""SessionManager 的主体解析 —— 链路最后一米(R2)。

设计 §13 把 P3 记作「``enforcer.py`` + ``execute_sql`` 接入 + 迁移夹具」。强制点
本身在 ``execute_sql``,但它拿到的 ``state.principal`` **由这里注入** —— 也就是说
I2(缺主体即拒绝)真正的失效点是「链路某处把主体丢了」。本文件守的就是那一段:
从会话身份 → 主体 → 落进 state。

两条方向相反的纪律在这里同时被钉住:

* **不许把主体存进会话复用。** grants 会变(管理台加/撤授权),而会话能存活数天。
  本文件的 ``test_grants_are_reread_every_run`` 就是防这一条回归。
* **解析不出来就是 ``None``(没有主体),不是「不设限」。** 见 :class:`Principal`
  的三行表 —— 拿不到依据与依据为空是两个方向。
"""

from __future__ import annotations

import pytest

from trove.core.types import Session
from trove.services.authz.policy import Policy, principal_from_wire


class _FakeAuth:
    """最小 auth 替身:用户表 + grants 表各一份 dict。

    只实现被用到的两处 —— ``store.get_user_by_id``(解析主体)与
    ``get_datasources``(取依据)。后者由 ``Policy.principal_for`` 独占调用。
    """

    def __init__(self, users=None, grants=None, *, explode=False):
        self.users = users or {}
        self.grants = grants or {}
        self.explode = explode
        self.store = self

    async def get_user_by_id(self, uid):
        if self.explode:
            raise RuntimeError("app.db is gone")
        return self.users.get(uid)

    async def get_datasources(self, user_id):
        if self.explode:
            raise RuntimeError("app.db is gone")
        return list(self.grants.get(user_id, []))


ADMIN_ROW = {"id": 1, "username": "root", "role": "admin"}
USER_ROW = {"id": 7, "username": "alice", "role": "user"}


@pytest.fixture
async def manager_with_auth(tmp_home, agent_config, graphs):
    """带 auth 的 SessionManager(conftest 那份刻意不带,见下)。"""
    from trove.agent.session import SessionManager
    from trove.storage.session_store import SessionStore

    store = SessionStore(home_dir=str(tmp_home))
    manager = SessionManager(
        config=agent_config,
        session_store=store,
        graphs=graphs,
        llm_gateway=None,
    )
    yield manager, store
    await store.dispose()


def _bind(manager, **kwargs) -> _FakeAuth:
    auth = _FakeAuth(**kwargs)
    manager._auth = auth
    return auth


class TestPrincipalResolution:
    async def test_no_auth_service_is_local_admin(self, manager_with_auth):
        """无 auth 服务 = 本机可信(嵌入 / 测试),与 ``deps.NullAuth`` 同口径。

        conftest 的 ``session_manager`` / CLI / stdio MCP 都走这条:它们没有
        auth 或没有远程身份,但要求它们先配一套账号才能查数才是反常的。
        """
        manager, _ = manager_with_auth
        principal = principal_from_wire(await manager._principal_wire(Session()))
        assert principal is not None
        assert principal.role == "admin"
        assert principal.subject == Policy.local_admin().subject

    async def test_local_session_is_local_admin(self, manager_with_auth):
        """``user_id="local"`` 是**无远程身份**的哨兵值,不是某个用户名。

        用户表主键是整数(``get_user_by_id(int(uid))``),所以真实用户永远撞不上
        这个字符串 —— 这是本条规则安全的前提,改身份存储时要重新确认。
        """
        manager, _ = manager_with_auth
        _bind(manager, users={1: ADMIN_ROW})
        session = Session(user_id="local")
        principal = principal_from_wire(await manager._principal_wire(session))
        assert principal is not None
        assert principal.role == "admin"

    async def test_remote_user_gets_its_role_and_grants(self, manager_with_auth):
        manager, _ = manager_with_auth
        _bind(manager, users={7: USER_ROW}, grants={7: ["sales"]})
        principal = principal_from_wire(
            await manager._principal_wire(Session(user_id="7"))
        )
        assert principal is not None
        assert principal.subject == "7"
        assert principal.role == "user"
        assert principal.grants == frozenset({"sales"})

    async def test_admin_role_needs_no_grants(self, manager_with_auth):
        """admin 不受 grants 限制 —— 取依据这一步对 admin 根本不发生。"""
        manager, _ = manager_with_auth
        auth = _bind(manager, users={1: ADMIN_ROW})
        auth.grants = None  # 被碰到就炸
        principal = principal_from_wire(
            await manager._principal_wire(Session(user_id="1"))
        )
        assert principal is not None
        assert principal.is_admin

    async def test_grants_are_reread_every_run(self, manager_with_auth):
        """撤权必须对**已存在的会话**立刻生效。

        把主体钉进 Session(它会被持久化、能活数天)会让「撤掉 grants」对老会话
        永远不生效 —— 那是一个安静的提权窗口。路由层同样是每请求现算
        (``deps.get_principal`` 缓存在 ``request.state``,不是 session)。
        """
        manager, _ = manager_with_auth
        _bind(manager, users={7: USER_ROW}, grants={7: ["sales"]})
        session = Session(user_id="7")

        before = principal_from_wire(await manager._principal_wire(session))
        assert before.grants == frozenset({"sales"})

        manager._auth.grants[7] = []  # 管理台撤权
        after = principal_from_wire(await manager._principal_wire(session))
        assert after.grants == frozenset()


class TestNoPrincipalIsNotUnrestricted:
    """I7:拿不到主体 → ``None`` → 执行层拒绝。**不是**「不设限」。"""

    async def test_unknown_user_id_yields_no_principal(self, manager_with_auth):
        manager, _ = manager_with_auth
        _bind(manager, users={7: USER_ROW})
        assert await manager._principal_wire(Session(user_id="999")) is None

    async def test_non_numeric_user_id_yields_no_principal(self, manager_with_auth):
        """既不是用户 id 也不是本地哨兵 —— 无从判定,不猜。"""
        manager, _ = manager_with_auth
        _bind(manager, users={7: USER_ROW})
        assert await manager._principal_wire(Session(user_id="alice")) is None

    async def test_missing_user_id_yields_no_principal(self, manager_with_auth):
        manager, _ = manager_with_auth
        _bind(manager, users={7: USER_ROW})
        assert await manager._principal_wire(Session(user_id="")) is None

    async def test_store_failure_is_not_translated_into_a_verdict(
        self, manager_with_auth,
    ):
        """基础设施故障照抛,不翻译成授权结论(``Policy.principal_for`` 的取舍)。

        翻成放行是提权,翻成拒绝是排障方向错 —— 两者都是把「不知道」伪装成
        「知道」。往上抛,调用方报错。
        """
        manager, _ = manager_with_auth
        _bind(manager, explode=True)
        with pytest.raises(RuntimeError):
            await manager._principal_wire(Session(user_id="7"))


class TestPrincipalReachesTheState:
    """R2 的回归测:主体必须真的走到 ``state`` 上(链路某处丢了就白做)。"""

    async def test_ask_injects_principal_into_state(self, manager_with_auth):
        manager, _ = manager_with_auth
        _bind(manager, users={7: USER_ROW}, grants={7: ["sales"]})
        session = await manager.start_session(project_cwd="/tmp/p", user_id="7")

        state = await manager.ask(
            session=session,
            question="What students are in Alameda county?",
            workflow_name="reflection",
        )
        principal = principal_from_wire(state.principal)
        assert principal is not None, "主体在链路上丢了 —— I2 会因此拒绝一切"
        assert principal.subject == "7"
        assert principal.grants == frozenset({"sales"})
