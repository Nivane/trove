"""数据源授权策略的**唯一实现** —— 主体模型与判定规则。

为什么单独成模块:同一条策略原先有**四份副本** —— ``api/deps.require_datasource``、
``mcp/server._granted``、``api/routers/catalog`` 的 grants 过滤、以及它们的
admin / scope 分支。四份今天结论一致纯属维护纪律,而漂移**已经发生过**:
``mcp`` 那份多一层「有身份但拿不到 auth 服务 → 按空 grants」的兜底,``deps``
那份走 ``NullAuth``(空 grants → 只放行默认源),两者对「同一个没有授权依据的
用户」给出不同答案。设计文档 §2.2 G1 / §5.2 记录的正是这件事。

本模块收敛的不只是「判定」,还有**取数权**:``get_datasources`` 全仓库只在
:meth:`Policy.principal_for` 里调用一次。四处各判各的还能靠纪律对齐,四处各
**取**各的则连比较的基准都不存在 —— 一致性测试没有意义,因为两边读的可能
根本不是同一份 grants。

不变量(设计文档 §4):
- **I6 只能收窄**:见 :meth:`Principal.narrow`。
- **I7 默认拒绝**:证据缺失的方向一律是拒绝。:class:`Principal` 的字段默认值
  因此是 fail-closed 的 —— ``Principal("u1")`` 什么数据源都不放行,要放行必须
  显式写成 admin。

与只读门的方向差异(设计 §8.1):只读门 fail-open,因为数据库侧只读角色是硬
边界、有兜底;授权门 fail-closed,因为**没有任何东西**在 SQL 落库前检查
「这个用户能不能碰这张表」。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:  # 只用于类型标注:策略模块保持零运行时依赖(见 may_bypass_masking)
    from trove.services.semantic_layer.models import MaskingPolicy

__all__ = [
    "LOCAL_SUBJECT",
    "Policy",
    "Principal",
    "may_bypass_masking",
    "principal_from_wire",
    "principal_to_wire",
    "scopes_allow",
    "visible_datasources",
    "visible_topics",
]

#: 「本机可信身份」的主体名 —— CLI / stdio MCP / ``allow_null_auth`` 的嵌入调用。
#:
#: 与 ``core/types.Session.user_id`` 的默认值、``api/deps.LOCAL_ADMIN["id"]``
#: 是同一个哨兵值,所以定义在这里而不是让各处再写一遍字面量:会话层要靠它
#: 判断「这一轮到底有没有远程身份」(见 ``agent/session._principal_wire``),
#: 三处各写各的迟早会漂移成一个安全的空洞。
#:
#: 安全前提:**用户表主键是整数**(``store.get_user_by_id(int(uid))``),真实
#: 用户永远造不出这个字符串。改身份存储(比如换成 UUID 主键或允许自定义
#: 用户名作 id)时必须重新确认这条。
LOCAL_SUBJECT = "local"


def visible_datasources(
    principal: "Principal", names: Iterable[str], default: str | None,
) -> list[str]:
    """``names`` 里该主体可见的那些,保持原顺序。

    数据源可见性是**一处规则**:catalog 列表页与 MCP 的 ``list_datasources``
    工具都调它,两边不会再各自写一遍过滤条件 —— 原先那两处就是这么分叉的
    (一个用 ``if not grants`` 判空、一个用 ``(restricted, allowed)`` 元组)。

    纯函数,不需要 :class:`Policy` 实例(它只管构造主体)。
    """
    return [n for n in names if principal.allows_datasource(n, default)]


def visible_topics(
    principal: "Principal", datasource: str, names: Iterable[str],
) -> list[str]:
    """``names`` 里该主体在 ``datasource`` 上可见的主题域,保持原顺序。

    与 :func:`visible_datasources` 同一条纪律:过滤规则只写一遍。主题域清单
    端点(``GET /v1/semantic/topics``)与拒绝文案里的 ``available_topics``
    都调它 —— 后者尤其要命:两处各判各的,「不可见」与「不存在」的拒绝文案
    就会分叉,等于把域的存在性泄露给受限用户(存在性预言机)。
    """
    return [n for n in names if principal.allows_topic(datasource, n)]


def scopes_allow(scopes: Iterable[str] | None, *required: str) -> bool:
    """token 级最小权限的规则本体。

    **未声明 scopes 的 token = 不限**(存量兼容:早于 ``scopes`` 字段签发的
    token 不该因为新增这个字段而失效);声明了 scopes 的受限 token 必须命中
    至少一个 ``required``。

    这条「空 = 不限」的规则**只在这里定义一次**。原先 ``require_admin`` 用
    ``in``、``require_scope`` 用 ``&`` 各写了一遍 —— 单元素时等价,多元素时
    就是两个语义,而两处读的都是同一个 token。
    """
    if not required:
        return True
    declared = frozenset(scopes or ())
    if not declared:
        return True
    return bool(declared & set(required))


def may_bypass_masking(principal: "Principal", policy: "MaskingPolicy | None") -> bool:
    """这个主体能不能看 PII 原文(设计 §5.5 / §7.1)。

    规则两条,与 :func:`scopes_allow` 的宽方向**刻意相反**:

    1. ``principal.scopes`` 与 ``policy.bypass_scopes`` **原始集合求交**非空 → True;
    2. 否则 ``policy.default_policy == "bypass"`` → True;其余(含认不出的取值)→ False。

    **第 1 条为什么不能用 ``scopes_allow`` / ``has_scope``。** 那条规则的
    「空 scopes = 不限」是给**路由门**准备的存量兼容:早于 ``scopes`` 字段签发的
    token 不该因为新增字段而失效,方向是**宽**。这条门的空 scopes 必须意味着
    「没有 pii」—— 否则每一个存量 token 都直接看到原文;更要命的是设计 §5.6 的
    ``on_behalf_of`` 重放(admin 排障)也会一起看原文,那个功能就成了 admin 的
    越权读取通道。判据沿用 §8.1:有兜底的方向可以宽(路由门),没兜底的方向必须
    严(PII 披露)。所以这里只做集合运算,不调 ``scopes_allow``。

    ``role`` 不参与判定(§8.4):admin 是**运维**角色不是**数据授权**角色,看原文
    要显式持 scope —— 这样「谁签发过看得见原文的凭证」在 token 侧就有记录。

    刻意不 import ``MaskingPolicy``(只在 ``TYPE_CHECKING`` 下引):本函数只读
    ``bypass_scopes`` / ``default_policy`` 两个属性,duck typing 够用,而
    ``policy.py`` 是**唯一实现**模块,运行时依赖越少越好。``policy=None``
    (没有声明)按「不 bypass」处理 —— 判据缺失的方向是严。
    """
    if policy is None:
        return False
    raw = getattr(policy, "bypass_scopes", None)
    if isinstance(raw, str):
        # 声明写成了标量(YAML 里少一层列表 -- 解析层若没拦住)。按「一个都
        # 没声明」处理,而不是把字符串拆成字符集:后者会悄悄放行一个谁也不
        # 认识的 scope 集合,前者只是不给 bypass(方向仍是严)。
        raw = ()
    declared = {str(scope).strip() for scope in (raw or ())}
    declared.discard("")
    if declared & set(principal.scopes or ()):
        return True
    default = str(getattr(policy, "default_policy", "") or "").strip().lower()
    return default == "bypass"


@dataclass(frozen=True)
class Principal:
    """一次请求的授权主体。

    **agent 没有独立身份**:它继承提问者的 ``Principal``(设计 §5.1 / N3)。
    给 agent 单独发一个 service account 会造出「agent 比用户权限大」的提权面,
    那正是要防的权限扩散。

    ``grants`` 的三种取值是三个**不同**的语义,别混:

    ====================  ==========================================
    ``None``(且非 admin)  **没有授权依据** → 拒绝一切(不查任何数据源)
    ``frozenset()``       有依据,依据为空 → 只放行默认源
    ``frozenset({...})``  非空 → 严格 allowlist
    ====================  ==========================================

    第一行最容易被写错成「不设限」。存量实现里 ``mcp`` 那份就是把它当成空
    grants 处理 —— 用户 grants 明明是 ``{sales}``、默认源是 ``financial`` 时,
    存储一抖动他就拿到了 financial。**拿不到依据 != 依据为空**,前者必须拒绝。
    要表示「不设限」只有一条路:``role="admin"``。

    ``topic_grants`` 是数据源门之下的**第二层可选收窄**(主题域可见性),
    取值也三态,但 ``None`` 的方向与 ``grants`` 刻意相反:

    ===========================  ==========================================
    ``None``(且非 admin)         **未配置收窄** → 可见源的全部主题域
    ``{}`` / 各源空集            已配置,清单为空 → 一个域都不见
    ``{ds: frozenset({...})}``   严格 allowlist;``ds`` 不在字典里 = 该源无域
    ===========================  ==========================================

    第一行为什么不是拒绝:主题域是叠加在**已经 fail-closed 的数据源门**之上
    的收窄层,「没配置」只该是「没收窄」—— 读成拒绝会让本功能上线的一刻,
    所有存量用户的主题域凭空消失。要表达「一个域都不见」有明确写法(``{}``)。
    第三行承接 ``grants`` 的纪律:声明了清单就是**完整**清单,漏写的源即
    拒绝(没有「默认域」概念,别照搬 grants 空集放行默认源的写法),方向仍是严。
    """

    subject: str
    role: str = "user"
    scopes: frozenset[str] = frozenset()
    #: None = 无授权依据(拒绝);见类 docstring 的三行表
    grants: frozenset[str] | None = None
    on_behalf_of: str | None = None
    #: None = 未配置收窄;见类 docstring 的第二张表
    topic_grants: dict[str, frozenset[str]] | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def has_scope(self, *required: str) -> bool:
        """token 级最小权限判定 —— 委托 :func:`scopes_allow`(规则只有一份)。"""
        return scopes_allow(self.scopes, *required)

    def allows_datasource(self, name: str, default: str | None) -> bool:
        """这个主体能不能碰数据源 ``name``。

        ``default`` 是注册表的默认源名 —— 空 grants 时唯一放行的那个。单数据源
        部署因此不需要预先配 grant 就能用。
        """
        if self.is_admin:
            return True
        if self.grants is None:
            return False          # 无授权依据 → 拒绝(I7)
        if self.grants:
            return name in self.grants
        return bool(default) and name == default

    def allows_topic(self, datasource: str, topic: str) -> bool:
        """这个主体能不能看见/使用 ``datasource`` 上的主题域 ``topic``。

        只答「域这一层」——数据源本身可不可见由 :meth:`allows_datasource`
        在各自入口先行判定(清单端点先过 ``require_datasource``、提问先过
        会话层的源授权),这里不重复做,也做不了:``allows_datasource``
        需要注册表默认源名,那个信息在域判定点上并不存在。

        admin 判定在读 ``topic_grants`` 之前(同 ``grants`` 的既有语义)。
        """
        if self.is_admin:
            return True
        if self.topic_grants is None:
            return True          # 未配置收窄(见类 docstring 的第二张表)
        return str(topic) in self.topic_grants.get(str(datasource), frozenset())

    def narrow(self, **overrides: Any) -> "Principal":
        """派生一个**不比自己更宽**的主体(I6)。

        agent 在流程中途(如 refuse 节点起草扩展)需要构造一个受限视图时用它。
        ``narrow`` 不是 ``replace``:``role`` 只能向下(admin → user),
        ``scopes`` / ``grants`` / ``topic_grants`` 只能取交集(域层因为
        ``None`` 是最宽取值,把 ``None`` 当收窄目标会**抛错**而不是放宽)——
        否则「收窄」就成了提权的后门。

        Raises:
            ValueError: 试图把 ``role`` 抬到 ``user`` 之上,或传入未知字段。
        """
        allowed = {"role", "scopes", "grants", "topic_grants", "on_behalf_of",
                   "subject"}
        unknown = set(overrides) - allowed
        if unknown:
            raise ValueError(f"narrow() 不认识的字段: {sorted(unknown)}")

        changes: dict[str, Any] = {}
        if "role" in overrides:
            role = str(overrides["role"])
            if self.is_admin and role != "admin":
                changes["role"] = role          # admin → user:收窄
            elif role != self.role:
                raise ValueError(f"narrow() 不能把 role 从 {self.role} 抬到 {role}")
        if "scopes" in overrides:
            changes["scopes"] = self.scopes & frozenset(overrides["scopes"] or ())
        if "grants" in overrides:
            asked = overrides["grants"]
            if asked is None:
                # 收窄成「无依据」= 拒绝一切,是合法方向
                changes["grants"] = None
            elif self.is_admin and self.grants is None:
                # admin 原本不受限:交集就是对方给的集合本身
                changes["grants"] = frozenset(asked)
            elif self.grants is None:
                changes["grants"] = None
            else:
                changes["grants"] = self.grants & frozenset(asked)
        if "topic_grants" in overrides:
            asked = overrides["topic_grants"]
            if asked is None:
                # 与 grants 不同:域层的 None 是**最宽**的取值,收窄不往这边走
                if self.topic_grants is not None:
                    raise ValueError(
                        "narrow() 不能把 topic_grants 从受限放宽为不限制")
            else:
                norm = {
                    str(ds): frozenset(ts) for ds, ts in dict(asked).items()
                }
                if self.topic_grants is None:
                    # 原本不受限(或 admin):交集就是对方给的清单本身
                    changes["topic_grants"] = norm
                else:
                    # 按源求交:任一侧没列出的源 = 那一侧允许空集,交集为空
                    merged: dict[str, frozenset[str]] = {}
                    for ds in set(self.topic_grants) | set(norm):
                        both = (self.topic_grants.get(ds, frozenset())
                                & norm.get(ds, frozenset()))
                        if both:
                            merged[ds] = both
                    changes["topic_grants"] = merged
        for key in ("on_behalf_of", "subject"):
            if key in overrides:
                changes[key] = overrides[key]
        return replace(self, **changes)


def principal_to_wire(principal: "Principal | None") -> dict[str, Any] | None:
    """主体 → 纯 JSON 形状(str/int/list/dict),跨节点边界用。

    **为什么不直接把 dataclass 放进 state。** 设计 §6.2 原写
    ``principal: Principal | None``,实测踩了 ``contract.py`` 记录过的同一个
    坑:LangGraph 每个超级步都拿 state 过一次 ``JsonPlusSerializer``,未注册的
    dataclass 在非严格模式下降级、在 ``LANGGRAPH_STRICT_MSGPACK=true`` 下被拦。
    降级形态是**普通 dict** —— 而执行层的判定点恰好调
    ``.allows_datasource()``,拿到 dict 就是 ``AttributeError``。安全判定点
    不该拿到意料之外的形状,所以形状由这里钉死。

    集合排序后落成 list:迭代顺序固定,checkpoint 之间可比、测试可断言。
    """
    if principal is None:
        return None
    return {
        "subject": principal.subject,
        "role": principal.role,
        "scopes": sorted(principal.scopes),
        # 三种取值原样保留 —— None / [] / [...] 是三个语义,塌陷即改向
        "grants": None if principal.grants is None else sorted(principal.grants),
        "on_behalf_of": principal.on_behalf_of,
        # 域层同理三态(那边是 None / {} / {...});键序固定、值排序,可比可断言
        "topic_grants": None if principal.topic_grants is None else {
            str(ds): sorted(ts)
            for ds, ts in sorted(principal.topic_grants.items())
        },
    }


def principal_from_wire(data: Any) -> "Principal | None":
    """wire → 主体;形状异常一律 ``None``(= 没有主体 → 拒绝)。

    **不「尽量解出一部分」**:半个主体在这里不是宽容,是**放宽** —— 漏掉
    ``role`` 会丢掉 admin,漏掉 ``grants`` 会把「有依据」读成「没有依据」
    (方向恰好相反)。``None`` 是明确的「没有主体」信号,由调用方按 I7 拒绝。
    这与 ``contract_from_wire`` 的取舍一致。
    """
    if not isinstance(data, dict):
        return None
    subject = data.get("subject")
    role = data.get("role", "user")
    if not isinstance(subject, str) or not subject:
        return None
    if not isinstance(role, str) or not role:
        return None
    scopes = _str_frozenset(data.get("scopes", ()))
    if scopes is None:
        return None
    raw_grants = data.get("grants")
    if raw_grants is None:
        grants: frozenset[str] | None = None
    else:
        grants = _str_frozenset(raw_grants)
        if grants is None:
            return None
    on_behalf_of = data.get("on_behalf_of")
    if on_behalf_of is not None and not isinstance(on_behalf_of, str):
        return None
    raw_topic_grants = data.get("topic_grants")
    if raw_topic_grants is None:
        # 键不存在(写下这份 checkpoint 时功能还不存在)与显式 null 同解:
        # 未配置收窄。按「历史行为」还原,而不是按今天的配置倒推 —— 主体
        # 是**运行时快照**,与 grants 同一条纪律(改授权不影响已在跑的会话)。
        topic_grants: dict[str, frozenset[str]] | None = None
    else:
        topic_grants = _topic_grants_from_wire(raw_topic_grants)
        if topic_grants is None:
            return None
    # 未知键忽略:旧代码读新 checkpoint 不该因为多一个字段而整个作废
    return Principal(
        subject=subject,
        role=role,
        scopes=scopes,
        grants=grants,
        on_behalf_of=on_behalf_of,
        topic_grants=topic_grants,
    )


def _topic_grants_from_wire(value: Any) -> dict[str, frozenset[str]] | None:
    """``{ds: [topic, ...]}`` 的 wire 形状 → 字典;形状不对则 ``None``(主体作废)。

    **空字典是合法取值**(显式「一个域都不见」),与形状错误必须分清 ——
    把 ``{}`` 读成错误会让一份「已收窄到零」的主体悄悄放宽成「未收窄」。
    """
    if not isinstance(value, dict):
        return None
    out: dict[str, frozenset[str]] = {}
    for ds, topics in value.items():
        if not isinstance(ds, str) or not ds:
            return None
        bag = _str_frozenset(topics)
        if bag is None:
            return None
        out[ds] = bag
    return out


def _str_frozenset(value: Any) -> frozenset[str] | None:
    """字符串集合的 wire 形状 → frozenset;不是字符串序列则 ``None``。

    ``str`` 本身是可迭代的,不先排除会把它拆成单个字符的集合 —— 那是把
    一个明显的形状错误静默翻译成一个看起来很合理的主体。
    """
    if isinstance(value, (str, bytes)) or not isinstance(
        value, (list, tuple, set, frozenset)
    ):
        return None
    if not all(isinstance(item, str) for item in value):
        return None
    return frozenset(value)


class Policy:
    """构造 :class:`Principal` 并列出可见数据源。

    ``auth`` 是 ``AuthService``(或 :class:`~trove.api.deps.NullAuth`)。它可以为
    ``None`` —— 那意味着**没有授权依据**,非 admin 主体因此拿到 ``grants=None``
    并被拒绝,而不是被当成不受限。这是 :meth:`principal_for` 里唯一一处需要
    小心的地方。
    """

    def __init__(self, auth: Any | None = None) -> None:
        self._auth = auth

    async def principal_for(
        self, user: dict[str, Any], *, on_behalf_of: str | None = None,
    ) -> Principal:
        """用户 dict(``get_current_user`` / ``resolve_token`` 的产物)→ 主体。

        **全仓库唯一的 ``get_datasources`` / ``get_topic_grants`` 调用点。**

        不捕获存储异常:取不到 grants 时往上抛(HTTP 500),而不是翻译成一个
        授权结论。基础设施故障不是「这个用户没有权限」,把它渲染成 403 会让
        排障方向整个错掉,渲染成放行则是提权。沿用 ``deps`` 原有的传播行为,
        ``mcp`` 原先的兜底(捕获 → 空 grants)**会放宽**(见 :class:`Principal`)。
        """
        role = str(user.get("role") or "user")
        scopes = frozenset(user.get("scopes") or ())
        grants: frozenset[str] | None = None
        topic_grants: dict[str, frozenset[str]] | None = None
        if role != "admin":
            if self._auth is not None:
                grants = frozenset(await self._auth.get_datasources(user.get("id")) or ())
                raw = await self._auth.get_topic_grants(user.get("id"))
                if raw is not None:
                    topic_grants = {
                        str(ds): frozenset(str(t) for t in topics or ())
                        for ds, topics in dict(raw).items()
                    }
            # auth 缺失 → grants 保持 None = 无依据 → 拒绝一切源;域层保持
            # None(未配置收窄)—— 没有源可见时域判定根本到不了
        return Principal(
            subject=str(user.get("id")),
            role=role,
            scopes=scopes,
            grants=grants,
            on_behalf_of=on_behalf_of,
            topic_grants=topic_grants,
        )

    @staticmethod
    def local_admin() -> Principal:
        """本机可信身份:stdio 挂载的 MCP、``allow_null_auth`` 的嵌入调用。

        与 ``deps.LOCAL_ADMIN`` / ``mcp`` 的「无 identity = 不设限」同口径。
        """
        return Principal(subject=LOCAL_SUBJECT, role="admin")

    @staticmethod
    def has_scope(principal: Principal, *required: str) -> bool:
        return principal.has_scope(*required)

    @staticmethod
    def may_bypass_masking(principal: Principal, policy: "MaskingPolicy | None") -> bool:
        """委托 :func:`may_bypass_masking`(规则只有一份)。"""
        return may_bypass_masking(principal, policy)

    @staticmethod
    def visible_datasources(
        principal: Principal, names: Iterable[str], default: str | None,
    ) -> list[str]:
        return visible_datasources(principal, names, default)

    @staticmethod
    def visible_topics(
        principal: Principal, datasource: str, names: Iterable[str],
    ) -> list[str]:
        return visible_topics(principal, datasource, names)
