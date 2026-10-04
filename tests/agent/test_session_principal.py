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

import json

import pytest

from trove.core.types import Session
from trove.services.authz.policy import Policy, principal_from_wire


class _FakeAuth:
    """最小 auth 替身:用户表 + grants / topic_grants 表各一份 dict。

    只实现被用到的几处 —— ``store.get_user_by_id``(解析主体)、
    ``get_datasources`` / ``get_topic_grants``(取依据)。两者都由
    ``Policy.principal_for`` 独占调用。
    """

    def __init__(self, users=None, grants=None, topic_grants=None, *,
                 explode=False):
        self.users = users or {}
        self.grants = grants or {}
        self.topic_grants = topic_grants or {}
        self.explode = explode
        self.audits: list[dict] = []
        self.store = self

    async def get_user_by_id(self, uid):
        if self.explode:
            raise RuntimeError("app.db is gone")
        return self.users.get(uid)

    async def get_datasources(self, user_id):
        if self.explode:
            raise RuntimeError("app.db is gone")
        return list(self.grants.get(user_id, []))

    async def get_topic_grants(self, user_id):
        if self.explode:
            raise RuntimeError("app.db is gone")
        raw = self.topic_grants.get(user_id)
        return None if raw is None else {k: list(v) for k, v in raw.items()}

    async def record_audit(self, action, user=None, **kwargs):
        self.audits.append({"action": action, "user": user, **kwargs})


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


class TestTokenScopesReachThePrincipal:
    """P4 前置(设计 §14 P3 五):token 的 scopes 必须送到主体上。

    会话路径原先恒为空 scopes —— 对 ``Authorizer`` 无影响(它不看 scopes),
    但脱敏的 ``bypass_scopes`` 会看,而两条错法都通向「谁都能看原文」:
    空 scopes 若按 ``scopes_allow`` 的「空 = 不限」判定,每个存量 token 与
    每次 ``on_behalf_of`` 重放都直接 bypass;而如果 scopes 根本送不到,
    持 ``pii`` 的 token 又永远看不见原文 —— 一个功能要么全开要么全关。
    修法只有一条:把 token 的 scopes 真的送到 ``principal`` 上。
    """

    async def test_scopes_from_the_token_land_on_the_principal(
        self, manager_with_auth,
    ):
        manager, _ = manager_with_auth
        _bind(manager, users={7: USER_ROW})

        wire = await manager._principal_wire(Session(user_id="7"), scopes=["pii"])

        assert wire is not None
        assert wire["scopes"] == ["pii"]

    async def test_no_scopes_stays_empty(self, manager_with_auth):
        """不传 scopes 的调用方(CLI / jobs / 测试)行为不变 —— 空集合。"""
        manager, _ = manager_with_auth
        _bind(manager, users={7: USER_ROW})

        wire = await manager._principal_wire(Session(user_id="7"))

        assert wire is not None
        assert wire["scopes"] == []

    async def test_ask_threads_scopes_into_state(self, manager_with_auth):
        """端到链路的回归:``ask(scopes=...)`` 必须出现在 ``state.principal`` 上。

        只测 ``_principal_wire`` 不够 —— 它和 ``ask`` 之间隔着一层状态构造,
        那正是 R2「链路某处把主体丢了」最容易发生的地方。
        """
        manager, _ = manager_with_auth
        _bind(manager, users={7: USER_ROW})
        session = await manager.start_session(project_cwd="/tmp/p", user_id="7")

        state = await manager.ask(
            session=session,
            question="What students are in Alameda county?",
            workflow_name="reflection",
            scopes=["pii"],
        )

        assert state.principal is not None
        assert state.principal["scopes"] == ["pii"]


    async def test_ask_threads_the_replay_into_state(self, manager_with_auth):
        """端到链路的回归:``ask(on_behalf_of=...)`` 必须出现在 ``state.principal``。

        与 scopes 那条同理 —— ``_principal_wire`` 与 ``ask`` 之间隔着状态构造,
        重放在那里丢掉的表现是「admin 以为自己看的是用户 42 的视图,其实是自己
        的」:一次**静默**的视图错位,比拒绝危险。
        """
        manager, _ = manager_with_auth
        _bind(manager, users={1: ADMIN_ROW, 7: USER_ROW})
        session = await manager.start_session(project_cwd="/tmp/p", user_id="1")

        state = await manager.ask(
            session=session,
            question="What students are in Alameda county?",
            workflow_name="reflection",
            on_behalf_of="7",
        )

        assert state.principal is not None
        assert state.principal["subject"] == "7"
        assert state.principal["on_behalf_of"] == "1"


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


class TestOnBehalfOfReplay:
    """P5 / 设计 §5.6 —— admin 以目标用户身份重放。

    「用户 A 看到的到底是什么」只能靠重放来答;而重放本身就是一个**读别人
    数据的通道**,所以它的每一处都必须 fail-closed:非 admin 用不了、
    目标不存在就拒绝、actor 的 scopes **一律丢掉**(§5.6 的 A8:不可与
    ``pii`` bypass 叠加)。最后一条是重点 —— 保留 admin 的 scopes 会让这个
    功能直接变成「admin 借用户 A 的名字读原文」,而那正是它要防的事。
    """

    async def test_target_is_the_subject_and_the_actor_is_recorded(
        self, manager_with_auth,
    ):
        manager, _ = manager_with_auth
        _bind(manager, users={1: ADMIN_ROW, 7: USER_ROW}, grants={7: ["sales"]})
        session = Session(user_id="1")  # 会话属于 admin

        wire = await manager._principal_wire(session, on_behalf_of="7")

        assert wire is not None
        assert wire["subject"] == "7"          # 看到的是目标用户的数据视图
        assert wire["role"] == "user"          # 连角色也按目标的走
        assert wire["grants"] == ["sales"]     # grants 取目标的
        assert wire["on_behalf_of"] == "1"     # 审计留的是**发起人**

    async def test_actor_scopes_are_dropped(self, manager_with_auth):
        """A8:重放不可与 ``pii`` bypass 叠加。

        admin 手里有 ``pii`` scope 时,「按目标用户的权限走」必须表现为
        **不带这个 scope** —— 否则重放视图里是原文,功能变成越权读取通道。
        """
        manager, _ = manager_with_auth
        _bind(manager, users={1: ADMIN_ROW, 7: USER_ROW})
        session = Session(user_id="1")

        wire = await manager._principal_wire(
            session, scopes=["pii"], on_behalf_of="7",
        )

        assert wire is not None
        assert wire["scopes"] == []

    async def test_replay_from_a_non_admin_session_is_refused(
        self, manager_with_auth,
    ):
        """非 admin 的会话请求重放 → ``None``(没有主体 → 执行层拒绝)。

        **不是**「静默忽略重放、按自己的身份跑」:那会把一次越权尝试变成一条
        看起来正常的查询。判定只认 auth 存储里的 role,不认任何入参。
        """
        manager, _ = manager_with_auth
        _bind(manager, users={1: ADMIN_ROW, 7: USER_ROW})
        assert await manager._principal_wire(
            Session(user_id="7"), on_behalf_of="1",
        ) is None

    async def test_replay_of_a_missing_target_is_refused(self, manager_with_auth):
        """目标用户不存在(或已被清理)→ 拒绝。**不是**退回按自己的身份跑。"""
        manager, _ = manager_with_auth
        _bind(manager, users={1: ADMIN_ROW})
        assert await manager._principal_wire(
            Session(user_id="1"), on_behalf_of="999",
        ) is None

    async def test_a_malformed_target_is_refused(self, manager_with_auth):
        """解析不出来的目标(非数字)→ 拒绝。无从判定时不猜(同 P3 的主体解析)。"""
        manager, _ = manager_with_auth
        _bind(manager, users={1: ADMIN_ROW})
        assert await manager._principal_wire(
            Session(user_id="1"), on_behalf_of="alice",
        ) is None

    async def test_the_api_spelling_is_accepted(self, manager_with_auth):
        """``"user:7"``(API 的拼写)与 ``"7"`` 同义 —— 收两种写法,一种行为。"""
        manager, _ = manager_with_auth
        _bind(manager, users={1: ADMIN_ROW, 7: USER_ROW})
        wire = await manager._principal_wire(Session(user_id="1"), on_behalf_of="user:7")
        assert wire is not None
        assert wire["subject"] == "7"

    async def test_an_unknown_subject_type_is_refused(self, manager_with_auth):
        """``"group:3"`` 不认识 → 拒绝,**不猜**(猜成用户 3 就是一次静默的身份
        替换)。多一种主体类型时这里必须有意识地改一次。"""
        manager, _ = manager_with_auth
        _bind(manager, users={1: ADMIN_ROW, 3: USER_ROW})
        assert await manager._principal_wire(
            Session(user_id="1"), on_behalf_of="group:3",
        ) is None

    async def test_the_replay_is_audited(self, manager_with_auth):
        """§5.6 的审计:``authz.on_behalf_of`` + ``{"actor", "on_behalf_of"}``。

        没有这条记录,「谁借了谁的身份」在审计面上不存在 —— 而重放的全部
        正当性都建立在「留痕」上。
        """
        manager, _ = manager_with_auth
        auth = _bind(manager, users={1: ADMIN_ROW, 7: USER_ROW})

        await manager._principal_wire(Session(user_id="1"), on_behalf_of="7")

        entry = next(e for e in auth.audits if e["action"] == "authz.on_behalf_of")
        assert entry["details"]["actor"] == "1"
        assert entry["details"]["on_behalf_of"] == "7"

    async def test_a_refused_replay_is_not_audited_as_a_replay(
        self, manager_with_auth,
    ):
        """被拒的重放不写 ``authz.on_behalf_of`` —— 那条记录的含义是「发生了
        一次重放」。被拒是一次**越权尝试**,由 P5 的 deny 轨记录。"""
        manager, _ = manager_with_auth
        auth = _bind(manager, users={1: ADMIN_ROW, 7: USER_ROW})

        await manager._principal_wire(Session(user_id="7"), on_behalf_of="1")

        assert [e for e in auth.audits if e["action"] == "authz.on_behalf_of"] == []

    async def test_no_replay_is_unchanged(self, manager_with_auth):
        """不传 ``on_behalf_of`` 的行为一字不变(存量路径回归)。"""
        manager, _ = manager_with_auth
        auth = _bind(manager, users={1: ADMIN_ROW, 7: USER_ROW})
        wire = await manager._principal_wire(Session(user_id="7"))
        assert wire["on_behalf_of"] is None
        assert wire["subject"] == "7"
        assert auth.audits == []


class TestAuthzAudit:
    """§6.3 的三条动作:``authz.deny`` / ``masking.applied`` / ``masking.bypass``。

    为什么是三条而不是一条:它们回答三个不同的问题 —— 「谁被拦了」「哪些列被
    改写了」「谁看了原文」。尤其后两条不能合并:一次 ``bypass`` 的运行
    ``fields`` 是**空的**(什么都没改),合并之后「admin 持 pii 看了身份证号」
    会表现为「这次没脱敏」,而那读起来像一句废话。

    **审计只记字段名 + 模式,不记值**(§6.3)。审计表是长期留存、可被管理端
    列出、会进备份的东西;把值写进去等于把脱敏要防的那份数据又抄了一份,
    而且是明文、没有 TTL 的那种。
    """

    async def test_a_denial_is_audited_with_its_reason(self, manager_with_auth):
        from trove.workflow.state import WorkflowState

        manager, _ = manager_with_auth
        auth = _bind(manager, users={7: USER_ROW})
        session = Session(session_id="s1", user_id="7")
        final = WorkflowState(
            session_id="s1", question="q", user_id="7",
            authz_decision={
                "allowed": False, "reason": "table",
                "narrowed_tables": ["salaries"], "datasource": "test_db",
            },
        )

        await manager._audit_authz(session, final)

        entry = next(e for e in auth.audits if e["action"] == "authz.deny")
        assert entry["details"]["reason"] == "table"
        assert entry["details"]["tables"] == ["salaries"]
        assert entry["details"]["datasource"] == "test_db"
        # 「谁被拦了」的「谁」:审计行上的 user 要回查到真名,不能只有 id
        assert entry["user"] == {"id": 7, "username": "alice"}

    async def test_a_warn_pass_is_not_a_denial(self, manager_with_auth):
        """warn 期放行的 A3 判定不写 ``authz.deny`` —— 放行不是拒绝(§8.2)。"""
        from trove.workflow.state import WorkflowState

        manager, _ = manager_with_auth
        auth = _bind(manager, users={7: USER_ROW})
        final = WorkflowState(
            session_id="s1", question="q", user_id="7",
            authz_decision={"allowed": True, "reason": "", "narrowed_tables": ["salaries"]},
        )

        await manager._audit_authz(Session(session_id="s1", user_id="7"), final)

        assert [e for e in auth.audits if e["action"] == "authz.deny"] == []

    async def test_a_warn_pass_is_audited_under_its_own_action(self, manager_with_auth):
        """放行的那一笔要**落库**,不是只进日志。

        §8.2 的观察期靠日志是收不齐的:一行一条的日志答不了「一周里哪些表」。
        审计表答得了,而且这正是它该记的事 —— 门看见了越界**并放行了**,比一次
        干脆的拒绝更值得留痕。
        """
        from trove.workflow.state import WorkflowState

        manager, _ = manager_with_auth
        auth = _bind(manager, users={7: USER_ROW})
        final = WorkflowState(
            session_id="s1", question="q", user_id="7",
            authz_decision={
                "allowed": True, "reason": "",
                "narrowed_tables": ["salaries", "bonuses"], "datasource": "test_db",
            },
        )

        await manager._audit_authz(Session(session_id="s1", user_id="7"), final)

        entry = next(e for e in auth.audits if e["action"] == "authz.table_warn")
        assert entry["details"]["tables"] == ["salaries", "bonuses"]
        assert entry["details"]["datasource"] == "test_db"
        assert entry["user"] == {"id": 7, "username": "alice"}

    async def test_a_clean_pass_writes_neither(self, manager_with_auth):
        """没命中就不写 —— 「放行了且没越界」不是审计事件,是常态。"""
        from trove.workflow.state import WorkflowState

        manager, _ = manager_with_auth
        auth = _bind(manager, users={7: USER_ROW})
        final = WorkflowState(
            session_id="s1", question="q", user_id="7",
            authz_decision={"allowed": True, "reason": "", "narrowed_tables": []},
        )

        await manager._audit_authz(Session(session_id="s1", user_id="7"), final)

        assert [e for e in auth.audits if e["action"].startswith("authz.")] == []

    async def test_masking_applied_records_fields_never_values(
        self, manager_with_auth,
    ):
        from trove.workflow.state import WorkflowState

        manager, _ = manager_with_auth
        auth = _bind(manager, users={7: USER_ROW})
        final = WorkflowState(
            session_id="s1", question="q", user_id="7",
            masking_applied={"fields": {"phone": "partial"}, "bypass": False},
        )

        await manager._audit_authz(Session(session_id="s1", user_id="7"), final)

        entry = next(e for e in auth.audits if e["action"] == "masking.applied")
        assert entry["details"]["fields"] == {"phone": "partial"}
        # 值一个都不在:13800138888 这类原文不该出现在审计表里
        assert "138" not in json.dumps(entry["details"], ensure_ascii=False)

    async def test_bypass_is_a_separate_action(self, manager_with_auth):
        """持 ``pii`` 的运行 ``fields`` 为空,但那正是最该留痕的一次。"""
        from trove.workflow.state import WorkflowState

        manager, _ = manager_with_auth
        auth = _bind(manager, users={1: ADMIN_ROW})
        final = WorkflowState(
            session_id="s1", question="q", user_id="1",
            masking_applied={"fields": {}, "bypass": True},
        )

        await manager._audit_authz(Session(session_id="s1", user_id="1"), final)

        actions = [e["action"] for e in auth.audits]
        assert "masking.bypass" in actions
        assert "masking.applied" not in actions

    async def test_a_run_that_changed_nothing_writes_nothing(self, manager_with_auth):
        """跑了、字段表为空 = 这个主体本来就不受限 → 不写 ``masking.applied``。

        空 ``fields`` 写进去会让「有多少次脱敏」和「有多少次跑了脱敏节点」
        变成同一个数,而后者在 warn 期/未配置期恒真 —— 一个恒真的审计项
        等于没有审计项。
        """
        from trove.workflow.state import WorkflowState

        manager, _ = manager_with_auth
        auth = _bind(manager, users={7: USER_ROW})
        final = WorkflowState(
            session_id="s1", question="q", user_id="7",
            masking_applied={"fields": {}, "bypass": False},
        )

        await manager._audit_authz(Session(session_id="s1", user_id="7"), final)

        assert auth.audits == []

    async def test_no_record_when_the_masking_step_never_ran(self, manager_with_auth):
        """``masking_applied is None`` = 这一步没跑 → 一条都不写。

        与 ``{"fields": {}, "bypass": False}``(跑了、什么都没改)是两个状态:
        前者不写审计是对的(没发生),后者不写也是对的(没改动)。
        """
        from trove.workflow.state import WorkflowState

        manager, _ = manager_with_auth
        auth = _bind(manager, users={7: USER_ROW})
        final = WorkflowState(session_id="s1", question="q", user_id="7")

        await manager._audit_authz(Session(session_id="s1", user_id="7"), final)

        assert auth.audits == []

    async def test_the_exchange_path_writes_them(self, manager_with_auth):
        """接线回归:``_record_exchange`` 是所有运行入口的汇合点(ask / resume /
        stream / 任务四条),审计挂在那里才对每条路径都生效。"""
        from trove.workflow.state import WorkflowState

        manager, _ = manager_with_auth
        auth = _bind(manager, users={7: USER_ROW})
        session = await manager.start_session(project_cwd="/tmp/p", user_id="7")
        final = WorkflowState(
            session_id=session.session_id, question="q", user_id="7",
            final_response="ok",
            masking_applied={"fields": {"id_card": "hash"}, "bypass": False},
        )

        await manager._record_exchange(session, "reflection", final)

        assert [e["action"] for e in auth.audits if e["action"].startswith("masking")] == [
            "masking.applied"
        ]

    async def test_query_execute_carries_the_replay_marker(self, manager_with_auth):
        """§5.6 的「审计双记」:重放的那次查询,``query.execute`` 里也要有标记。

        只在 ``authz.on_behalf_of`` 里记一次的话,「这条 SQL 是以谁的身份跑的」
        要看两张表拼起来才知道 —— 而排障的人先看的是查询记录。
        """
        from trove.workflow.state import WorkflowState

        manager, _ = manager_with_auth
        auth = _bind(manager, users={1: ADMIN_ROW, 7: USER_ROW})
        final = WorkflowState(
            session_id="s1", question="q", user_id="1", sql="SELECT 1",
            principal={"subject": "7", "role": "user", "scopes": [],
                       "grants": [], "on_behalf_of": "1"},
        )

        await manager._audit_query(Session(session_id="s1", user_id="1"), final)

        entry = next(e for e in auth.audits if e["action"] == "query.execute")
        assert entry["details"]["on_behalf_of"] == "1"


class TestCacheMaskingIsolation:
    """结果缓存 × 脱敏/授权的三处口子(P5 设计时查出)。

    缓存键是 ``(会话, 数据源, 归一化问句)``,而 ``_state_summary`` 会把
    ``rows`` / ``rows_preview`` 一起缓存 —— 也就是**数据本身**。P5 之前:

    1. 键里没有「以谁的身份」这个分量 —— 会话主人自己问一次、再以目标身份
       重放同一句,两次视图不同(脱敏范围不同)却共用一条缓存;
    2. bypass 的运行(持 ``pii`` 读了原文)照样写缓存 —— 最不该在内存里留一份
       的那个结果留了下来;
    3. ``_state_summary`` 不带 ``masking_applied`` —— 命中路径重建出的 state
       丢了脱敏报告,前端标记(P6)与 ``masking.applied`` 审计一起失效。

    修法是三条:键里加主体分量、bypass 不写缓存、报告进 summary。
    """

    @pytest.fixture
    async def cache_manager(self, tmp_home, graphs):
        """带 auth + 结果缓存的 manager(conftest 的 agent_config 默认不开缓存)。"""
        from trove.agent.session import SessionManager
        from trove.core.config import AgentConfig
        from trove.storage.session_store import SessionStore

        store = SessionStore(home_dir=str(tmp_home))
        manager = SessionManager(
            config=AgentConfig(
                home=str(tmp_home), target="mock/model", result_cache=True,
            ),
            session_store=store,
            graphs=graphs,
            llm_gateway=None,
        )
        yield manager
        await store.dispose()

    @staticmethod
    def _final(session, **over):
        from trove.workflow.state import WorkflowState

        state = {
            "session_id": session.session_id,
            "question": "how many students?",
            "user_id": session.user_id,
            "sql": "SELECT COUNT(*) FROM students",
            "row_count": 5,
            "verdict": "OK",
            "final_response": "5",
        }
        state.update(over)
        return WorkflowState(**state)

    async def test_the_view_is_part_of_the_key(self, cache_manager, tmp_home):
        """键的第四个分量 = 主体(``principal.subject``)。

        主体取自**已解析的** principal 而不是入参:重放时入参是发起人、主体是
        目标,而视图属于后者。
        """
        from trove.storage.session_store import SessionStore

        store = SessionStore(home_dir=str(tmp_home))
        cache_manager._session_store = store
        session = await cache_manager.start_session(project_cwd="/tmp/p", user_id="1")

        plain = cache_manager._cache_key(session, "q")
        replayed = cache_manager._cache_key(
            session, "q", principal={"subject": "7", "role": "user"},
        )
        assert plain != replayed
        assert plain[3] == ""          # 无主体(CLI / 无 auth)保持原样
        assert replayed[3] == "7"

    async def test_a_narrowed_scope_is_a_different_key(
        self, cache_manager, tmp_home,
    ):
        """TTL 内收窄授权 → 新键 → 必 miss(命中路径不跑图,不会重判范围)。

        场景:同一主体、同一问句、同一主题域,管理员在 300s TTL 内把主题域
        (或数据源)授权从「未收窄」改成清单 —— 旧结果是按**另一个可见范围**
        判出来的(可能已经端出过现在不该看到的行),回放它等于范围失效。
        ``subject`` 分量对此无感(收窄不换人),所以授权快照必须自成一个分量。
        """
        session = await cache_manager.start_session(
            project_cwd="/tmp/p", user_id="7")
        wide = {"subject": "7", "role": "user", "scopes": [], "grants": None,
                "topic_grants": None}
        narrowed = {**wide, "topic_grants": {"fin": ["loans"]}}

        old = cache_manager._cache_key(session, "q", None, wide, "loans")
        new = cache_manager._cache_key(session, "q", None, narrowed, "loans")
        assert old != new
        # 同快照无论构造几次同键(内容相等 → 键相等)
        assert cache_manager._cache_key(
            session, "q", None, dict(wide), "loans") == old
        # 数据源级 grants 的收窄同理(同一类洞,一起关)
        assert cache_manager._cache_key(
            session, "q", None, {**wide, "grants": ["fin"]}, "loans") != old
        # 具体到「收窄后不命中」:旧键存,新键取不到
        cache_manager._cache_put(old, {"question": "q"})
        assert cache_manager._cache_get(old) is not None
        assert cache_manager._cache_get(new) is None

    async def test_a_masked_run_is_still_cached(self, cache_manager, tmp_home):
        """反向:脱敏过的运行**照常缓存** —— 别把缓存整个关掉。

        缓存的数据已经是这个视图该看到的形状,丢了这条优化等于每次重复提问
        都重新打一次库。
        """
        session = await cache_manager.start_session(project_cwd="/tmp/p", user_id="7")
        final = self._final(
            session, masking_applied={"fields": {"name": "partial"}, "bypass": False},
        )

        cache_manager._maybe_cache_exchange(session, final)

        assert len(cache_manager._result_cache) == 1

    async def test_a_bypassed_run_is_not_cached(self, cache_manager, tmp_home):
        """bypass = 这次把原文交出去了。内存里不留副本。

        下次同问要重新走一遍脱敏节点(此刻的 scope 说了算),而不是把一份
        未经校验的原文再端一次。代价是重复提问多打一次库 —— 手持原文的
        查询不该由一条缓存优化替它承担风险。
        """
        session = await cache_manager.start_session(project_cwd="/tmp/p", user_id="1")
        final = self._final(
            session, masking_applied={"fields": {}, "bypass": True},
        )

        cache_manager._maybe_cache_exchange(session, final)

        assert cache_manager._result_cache == {}

    async def test_the_cached_entry_reconstructs_the_masking_report(
        self, cache_manager, tmp_home,
    ):
        """命中路径重建出的 state 必须带着脱敏报告。

        丢了它,前端下一次拿到 unmasked 的行却没有任何标记 —— 而标记是
        「这列已经被改写」的唯一出口(设计 §7.2 / P6)。
        """
        session = await cache_manager.start_session(project_cwd="/tmp/p", user_id="7")
        report = {"fields": {"name": "partial", "phone": "hash"}, "bypass": False}
        final = self._final(session, masking_applied=report)

        cache_manager._maybe_cache_exchange(session, final)
        cached = cache_manager._cache_get(
            cache_manager._cache_key(session, final.question, None, final.principal),
        )
        assert cached is not None
        hit = cache_manager._cached_final(cached, "run-2", "", "zh")

        assert hit.masking_applied == report

    async def test_a_cached_hit_keeps_who_the_data_was_produced_for(
        self, cache_manager, tmp_home,
    ):
        """命中路径也要知道「这份数据是以谁的身份产出的」。

        ``query.execute`` 的重放双记读的就是它;丢了主体,重放期间命中会写成
        「以发起人身份跑的」—— 一条**错的**审计,比缺一条更糟。
        """
        session = await cache_manager.start_session(project_cwd="/tmp/p", user_id="1")
        principal = {
            "subject": "7", "role": "user", "scopes": [], "grants": [],
            "on_behalf_of": "1",
        }
        final = self._final(session, principal=principal)

        cache_manager._maybe_cache_exchange(session, final)
        cached = cache_manager._cache_get(
            cache_manager._cache_key(session, final.question, None, principal),
        )
        hit = cache_manager._cached_final(cached, "run-2", "", "zh")

        assert hit.principal == principal
