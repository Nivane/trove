"""执行画像的**成本轨** —— 预算降级链与三路径决策(设计 §5.2 / §7.1)。

为什么单独成模块:现存的三道成本护栏（EXPLAIN 行数估算、软/硬上限、结果行数
截断）**全部 fail-open**,而它们防的东西和权限轨不是一回事(§2.3):

- 权限轨防**写操作**:误拒会阻断正常业务,而数据库侧只读角色是硬边界 → fail-open
  是对的,不动(``services/sql/guard.py`` 保持原样)。
- 成本轨防**打爆库**:误放行的代价是打爆生产库,而**没有任何下层兜底** —— 一个
  只读角色不会阻止一条扫 10TB 的 ``SELECT``。∴ 必须 fail-closed。

判据是「有没有下层兜底」,不是「哪个更危险」(§8.1)。

本模块只做**决策**,不做执行:``estimate`` 给出代价与它有多可信,``decide`` 给出
allow / degrade / reject。真正落库前的那一步在 ``workflow/nodes/execute_sql.py``。

不变量(§4):

- **I2 不得静默 fail-open**:任何一次「无法估算」都必须留下 ``degraded=True`` 的
  痕迹并进入答案 —— 这是本模块存在的全部理由。放行可以,但要**说出来**。
- **I3 估算失败时的默认方向是保守**:保守预算取该数据源配置的
  ``assume_max_scan_bytes``(默认 20GB,对齐市场 execution profile)。
- **I6 不因护栏本身故障阻断查询**:护栏内部异常 → 退到下一档 / 保守预算 + 记录,
  **不 raise 到用户**。注意 I6 是 I2 的边界而不是它的反面:可以「不拦」,不可以
  「不拦还说没事」。

与 ``row_guard`` 的分工:那个模块**逻辑不动**,它是纯估算器(``estimate_max_rows``
的 fail-open 返回值 ``None`` 是「算不出来」这一事实的忠实表达);本模块承接
``None`` 的**上游语义** —— 算不出来之后该往哪边走。
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from typing import Any

from trove.core.logging import get_logger
from trove.core.types import positive_int as _positive
from trove.services.limits import DEFAULT_MAX_ROWS
from trove.services.sql import row_guard

logger = get_logger(__name__)

__all__ = [
    "BudgetDecision",
    "BudgetService",
    "CostEstimate",
    "ExecutionBudget",
    "LimitedSql",
    "describe_cost",
    "execution_evidence",
    "force_limit",
]

#: ``on_unestimable`` 的两个取值。``degrade`` 是默认——理由见 §8.3 C:
#: 过严的护栏会被绕过(用户去直连库),那连观测都没了。
ON_UNESTIMABLE = ("degrade", "reject")


@dataclass(frozen=True)
class ExecutionBudget:
    """一次执行的预算(设计 §6.2)。

    与 ``limits.ResultLimits`` 的关系:``max_rows`` 同源(结果行上限),这里把它
    收进预算一起表达;``ResultLimits`` 不动,降级时由调用方取它的值填进来,
    避免两处各存一份「上限」。
    """

    max_rows: int = DEFAULT_MAX_ROWS
    timeout_ms: int = 30_000
    #: 超软限 → 打回 gen_sql 收窄(可修正);超硬限 → 直接拒(重写也降不下来)
    soft_scan_rows: int = 50_000_000
    hard_scan_rows: int = 1_000_000_000
    #: 估算不可得时的保守预算。按数据源可配(§11 R2:默认 20GB 可能过松,
    #: 上线后按 estimated/scanned 的差值校准,逐步收紧)。
    assume_max_scan_bytes: int = 20 * 1024**3
    #: 估算不可得时的处置:``degrade``(默认) | ``reject``
    on_unestimable: str = "degrade"
    #: 并发上限;0 = 不限(§3.2 N3:只做上限,不做队列)。P2。
    max_concurrent: int = 0

    def __post_init__(self) -> None:
        normalized = str(self.on_unestimable or "").strip().lower()
        if normalized not in ON_UNESTIMABLE:
            # 拼错就当场炸,不静默退回某一边:退回 degrade 是放宽一个本该严格的
            # 环境,退回 reject 是让一个本该降级的环境每次估算失败都拒服务 ——
            # 两个方向都不能替使用者决定。同 Authorizer 的 mode 校验。
            raise ValueError(
                f"budget.on_unestimable 只能是 {ON_UNESTIMABLE} 之一,"
                f"收到 {self.on_unestimable!r}"
            )
        if normalized != self.on_unestimable:
            object.__setattr__(self, "on_unestimable", normalized)


@dataclass(frozen=True)
class CostEstimate:
    """代价估算 + **它有多可信**。

    ``source`` 是给人读的,判定只认 ``estimated_rows`` —— 两者若不一致(比如
    适配器换实现后谎报 ``source="metadata"`` 却没给数),信数:没数就是没依据。
    """

    estimated_rows: int | None
    estimated_bytes: int | None
    #: "explain"(最准) | "metadata"(粗但有) | "conservative"(兜底)
    source: str
    #: ``source == "conservative"`` 的等价说法。I2 的载体:它要进答案。
    degraded: bool
    detail: dict = field(default_factory=dict)

    @property
    def has_basis(self) -> bool:
        """有没有可用的估算依据 —— ``0`` / 负数都不算(见 ``_positive``)。"""
        return _positive(self.estimated_rows) is not None


@dataclass(frozen=True)
class BudgetDecision:
    """``decide`` 的结论。

    比设计 §7.1 的 ``-> str`` 多两个字段,原因:``verdict`` 的三值词汇
    (allow/degrade/reject)不够用 —— 超软限与超硬限**都是 reject,但处置相反**
    (前者打回 gen_sql 收窄,后者直接拒,见 §8.5 保留双上限)。把它们塞回调用方
    各判一次,阈值就又有两处了,而那正是本方案要收的东西。
    """

    #: "allow" | "degrade" | "reject"
    verdict: str
    #: 仅 reject 有意义:"" | "soft" | "hard"
    over: str = ""
    reason: str = ""


class BudgetService:
    """估算降级链(§5.2)+ 决策。

    Args:
        budget: 预算;None → :class:`ExecutionBudget` 默认值。
        explain: ``(sql, datasource) -> QueryResult``(可 async)。生产传
            ``connectors.explain`` —— 它是规划不是取数,毫秒级。
        parse_explain: ``(dialect, QueryResult) -> int | None``;默认
            :func:`row_guard.estimate_max_rows`(**不改它的 fail-open 语义**)。
        metadata: ``(datasource, tables) -> int | None``(可 async),表行数画像的
            求和入口。与 ``explain`` 同参:**带数据源** —— 少了它,多数据源部署
            里画像查的是默认库,行数来自 A 库而 SQL 跑在 B 库上。未接时这一档
            直接跳过 → 保守预算(与 ``_build_authorizer`` 的 ``declared_tables``
            同一种「能力未接即跳过该档」的接法)。

    三个依赖都是**可注入的可调用对象**而不是具体服务:降级链是纯逻辑,不该为了
    测它去起一个数据库(§12 A9:零 LLM、零网络)。
    """

    def __init__(
        self,
        budget: ExecutionBudget | None = None,
        *,
        explain: Callable[[str, str], Any] | None = None,
        parse_explain: Callable[[str, Any], int | None] | None = None,
        metadata: Callable[[str, list[str]], Any] | None = None,
    ) -> None:
        self.budget = budget or ExecutionBudget()
        self._explain = explain
        self._parse = parse_explain or row_guard.estimate_max_rows
        self._metadata = metadata

    # ── 估算 ──────────────────────────────────────────────

    async def estimate(
        self, datasource: str, sql: str, dialect: str = "",
        tables: Iterable[str] | None = None,
    ) -> CostEstimate:
        """按可信度降序取第一个可得的依据(§5.2);都不可得 → 保守预算。"""
        known_tables = [t for t in (tables or []) if str(t or "").strip()]

        rows = await self._from_explain(sql, dialect, datasource)
        if rows is not None:
            return CostEstimate(rows, None, "explain", False,
                                {"datasource": datasource, "dialect": dialect})

        rows = await self._from_metadata(datasource, known_tables)
        if rows is not None:
            return CostEstimate(rows, None, "metadata", False,
                                {"datasource": datasource, "tables": known_tables})

        return CostEstimate(
            None, self.budget.assume_max_scan_bytes, "conservative", True,
            {
                "datasource": datasource,
                "tables": known_tables,
                "assume_max_scan_bytes": self.budget.assume_max_scan_bytes,
            },
        )

    async def _from_explain(
        self, sql: str, dialect: str, datasource: str,
    ) -> int | None:
        """第 1 档:EXPLAIN 估算。拿不到 / 解析不出 / 报错 → None(退下一档)。

        EXPLAIN 绑**这一轮的数据源**(与 ``connectors.explain(sql, datasource)``
        同参):不传的话会落到默认数据源上,在多数据源部署里估算的是**另一个库**
        的代价 —— 而那正好是本方案要防的那类安静错判。
        """
        if self._explain is None or not (sql or "").strip():
            return None
        try:
            plan = await _maybe_await(self._explain(sql, datasource))
            return _positive(self._parse(dialect, plan))
        except Exception as e:
            # I6:护栏自身故障不阻断查询,退到下一档 —— 但不静默:降级链的终点
            # 必然留下 degraded 痕迹,所以这里可以只 warn。
            logger.warning("budget: EXPLAIN 估算不可用(%s),降级到下一档", e)
            return None

    async def _from_metadata(
        self, datasource: str, tables: list[str],
    ) -> int | None:
        """第 2 档:元数据画像(表行数之和)。未接 / 一个表都不认识 → ``None``。

        这一档是**粗**的:按表行数求和,看不见 WHERE、看不见投影、更看不见
        LIMIT。粗到不能用来打回重生成(那不收敛,见 :meth:`decide`),但用来
        判断「这条查询是不是在扫一张十亿行的表」绰绰有余 —— 而那正是本模块
        唯一想拦的东西。
        """
        if self._metadata is None or not tables:
            return None
        try:
            return _positive(await _maybe_await(self._metadata(datasource, tables)))
        except Exception as e:
            logger.warning("budget: 元数据画像不可用(%s),降级到保守预算", e)
            return None

    # ── 决策 ──────────────────────────────────────────────

    def decide(
        self, est: CostEstimate, budget: ExecutionBudget | None = None,
    ) -> BudgetDecision:
        """代价 → allow / degrade / reject(§5.2 的处置表)。

        两个上限是**闭区间**:等于 soft 不算超。
        """
        b = budget or self.budget
        rows = _positive(est.estimated_rows)

        if rows is None:
            if b.on_unestimable == "reject":
                return BudgetDecision(
                    "reject", "",
                    "无法估算本次查询的扫描量,而本部署配置为拒绝无法估算的查询"
                    "(budget.on_unestimable=reject)。",
                )
            # 路径 3:本方案改的**唯一**一处行为 —— 放行 → 降级执行。
            # 不阻断业务(I6),但不再有零上限的黑洞(I2)。
            return BudgetDecision(
                "degrade", "",
                "无法估算本次查询的扫描量(EXPLAIN 与元数据画像都没有依据),"
                "按保守预算降级执行。",
            )

        if rows <= b.soft_scan_rows:
            return BudgetDecision("allow")
        if rows <= b.hard_scan_rows:
            if est.source == "metadata":
                # 画像档的估算**只能降级,不能打回重生成** —— 那是一个不收敛的环。
                #
                # 画像按表行数求和,看不见 WHERE、看不见投影、更看不见 LIMIT:
                # ``SELECT name FROM big LIMIT 10`` 与不带 LIMIT 的估算是同一个
                # 数。打回 gen_sql 之后下一轮判定完全相同,再打回……直到烧完
                # max_retries —— 在 ClickHouse(最需要护栏的大表方言)上就是每个
                # 查询白烧十轮 LLM 再失败。
                #
                # EXPLAIN 档不受影响:加 LIMIT 计划会变,环是收敛的(§5.2 的处置
                # 表对那一档依然逐字成立)。
                return BudgetDecision(
                    "degrade", "",
                    f"按元数据画像估算涉及 {rows} 行(表行数之和,看不见过滤条件),"
                    f"超过软上限 {b.soft_scan_rows} —— 按保守方式加 LIMIT 执行。",
                )
            return BudgetDecision(
                "reject", "soft",
                f"估算扫描 {rows} 行,超过软上限 {b.soft_scan_rows} —— 打回重生成,"
                "收窄过滤条件或加 LIMIT。",
            )
        return BudgetDecision(
            "reject", "hard",
            f"估算扫描 {rows} 行,超过硬上限 {b.hard_scan_rows} —— 直接拒绝,"
            "重生成降到硬限以下没有指望。",
        )


# ── describe_cost:生成前先问价(§7.2)──────────────────────


async def describe_cost(
    sql: str,
    *,
    budget: BudgetService,
    datasource: str = "",
    dialect: str = "",
    tables: Iterable[str] | None = None,
    profiles: Any = None,
    allowed_tables: set[str] | None = None,
) -> dict[str, Any]:
    """给一条 SQL 报价:估算 + 处置结论 + 涉及的表(设计 §7.2)。

    **用途**是省掉一次往返:agent 在写复杂查询**之前**先问一句代价,超限就当场
    改小;不问的话,代价要等到执行节点才被发现,那时已经烧了一整轮生成,还要
    再烧一轮重生成(当下 G6 的双份 LLM 调用)。所以这个工具要在生成**前**可见
    —— 藏在懒激活后面等于把问价挪到生成之后,正好错过它唯一要省的东西。

    与执行节点的关系:**同一套判定,两个时点**。这里调的是 :meth:`BudgetService.estimate`
    与 :meth:`BudgetService.decide`,与 ``execute_sql`` 逐字相同 —— 报价与放行
    若各判一次,模型会拿到一个「说允许、跑起来被拒」的答案,比不报价更糟。

    只读、零 LLM;SQL 先过 ``check_readonly``。解析失败**fail-open**(与执行路径
    同一条政策:方言盲区不等于权限违规,而且这里不执行任何东西,真边界在数据库
    侧只读角色)。写语句**不给报价**:返回 ``verdict="reject"`` 且**不去 EXPLAIN**
    —— EXPLAIN 本身就是一条要在库里跑的查询,为一条注定被拒的语句付它不是防御,
    是开销。

    Args:
        sql: 待报价的 SQL。
        budget: 预算服务。**必填** —— 没有它就没有阈值,也就没有 verdict 可言;
            未装配成本轨的部署(vendor 嵌入场景)不应该挂出这个工具。
        datasource / dialect: 与执行时**同一轮**的数据源与方言。少传数据源,
            多数据源部署里估的是另一个库的代价。
        tables: 涉及的表;``None`` → 从 SQL 解析(与 authz/预算同一份
            ``referenced_tables``,三处对「触及哪些表」必须一致)。
        profiles: 画像服务(鸭子类型,只用 ``table_profile``)。未接 → 表清单
            照给,数字全 ``None``(「不可得 ≠ 0」)。
        allowed_tables: 授权范围内的表名,透传给只读校验的 allowlist。

    Returns:
        §7.2 的 wire 形状。**没有 error 字段** —— 拒绝也是一种答复:写语句、
        无法估算、超硬限都走 ``verdict`` + ``reason``,模型不必学两套读法。

        两个"没有"要分清:``source=""`` 是**没估过**(只读校验挡下),
        ``degraded=True`` 是**估了但没依据**(退保守预算)。
    """
    from trove.services.sql.guard import check_readonly

    text = (sql or "").strip()
    names = _cost_tables(text, dialect, tables)

    ok, reasons = check_readonly(
        text, dialect or "", allowed_tables, fail_on_parse_error=False,
    )
    if not ok:
        return {
            "estimated_rows": None,
            "estimated_bytes": None,
            "source": "",
            # 不是「降级」:没有估算被做出来,也就无从降级 —— 报了 True 会让
            # 调用方以为还有一份保守预算在兜着。
            "degraded": False,
            "verdict": "reject",
            "tables": [],
            "reason": f"这条 SQL 没有通过只读校验:{'; '.join(reasons)}",
        }

    try:
        est = await budget.estimate(datasource, text, dialect, names)
        decision = budget.decide(est)
    except Exception as e:
        # I6:问价工具自己炸了不该把异常抛进生成循环。保守作答 —— 与
        # ``execute_sql._judge_budget`` 同一方向:守卫不可信时,恰恰最没有理由
        # 相信这次查询便宜。方向取设计 §8.3 C 的默认(degrade),因为读不到这个
        # 服务的配置(它已经不可用了)。
        logger.warning("describe_cost: 预算判定不可用(%s) — 按保守预算作答", e)
        return {
            "estimated_rows": None,
            "estimated_bytes": None,
            "source": "conservative",
            "degraded": True,
            "verdict": "degrade",
            "tables": await _cost_table_facts(profiles, datasource, names),
            "reason": f"预算判定不可用({e}),按保守预算处理。",
        }

    return {
        # I7:这两个数是**估算**,与执行后的实际分开落库(evidence 里的
        # scanned_rows / returned_rows),合并成一个字段就没法校估算器了。
        "estimated_rows": est.estimated_rows,
        "estimated_bytes": est.estimated_bytes,
        "source": est.source,
        "degraded": est.degraded,
        "verdict": decision.verdict,
        "tables": await _cost_table_facts(profiles, datasource, names),
        "reason": decision.reason,
    }


def _cost_tables(sql: str, dialect: str, tables: Iterable[str] | None) -> list[str]:
    """这条 SQL 触及的表(排序、小写、去 schema 前缀)。

    复用 authz 的 ``referenced_tables`` 而不是另写一个解析:授权、预算、报价
    三处对「查了哪些表」必须给出同一个答案 —— 不一致的那天会出现「授权认为查了
    A 表、报价认为没查」这种谁也说不清的账。解析不出来 → 空清单(报价里多一个
    猜出来的表名,比少一个更危险)。
    """
    if tables is None:
        from trove.services.authz.enforcer import referenced_tables

        found = referenced_tables(sql, dialect or "")
        return sorted(found or ())
    return sorted({str(t or "").strip().lower() for t in tables if str(t or "").strip()})


async def _cost_table_facts(
    profiles: Any, datasource: str, names: list[str],
) -> list[dict[str, Any]]:
    """每表一行:名字 + 画像里的行数/字节数,取不到就是 ``None``。

    画像查不到时**仍然给出表名** —— 名字是从 SQL 解出来的,不依赖画像;把整条
    记录省掉会让模型以为这句 SQL 没碰任何表。
    """
    out: list[dict[str, Any]] = []
    for name in names:
        row_count = size = None
        if profiles is not None:
            try:
                profile = await _maybe_await(profiles.table_profile(datasource, name))
                row_count = _positive(getattr(profile, "row_count", None))
                size = _positive(getattr(profile, "bytes", None))
            except Exception as e:
                logger.warning("describe_cost: 表画像不可用(%s)", e)
        out.append({"name": name, "row_count": row_count, "bytes": size})
    return out


# ── 强制 LIMIT(降级执行的落点)────────────────────────────


@dataclass(frozen=True)
class LimitedSql:
    """一条**已被限住**的 SQL,以及限住它的是哪个数。

    ``limit`` 单列出来而不是让调用方从 ``max_rows`` 推:两者在「SQL 自己已经带
    了更紧的 LIMIT」时**不相等**,而此时记 ``max_rows`` 就是谎报 —— 运维会以为
    这条查询被放到了 1000 行,实际只有 10 行,下一条查询的容量规划就建在这个错
    数上。
    """

    sql: str
    limit: int


def force_limit(sql: str, dialect: str, max_rows: int) -> LimitedSql | None:
    """给 ``sql`` 加一个不超过 ``max_rows`` 的顶层 LIMIT;做不到返回 ``None``。

    **返回 ``None`` 而不是原串**:调用方要靠「加没加上」来决定要不要记
    ``limit_applied``。静默返回原文是这里最危险的写法 —— 调用方以为有了边界,
    实际上一条全表扫描原样出去了。

    已有 LIMIT 时取**更紧的那个**:把 ``LIMIT 10`` 放宽到 1000 等于借降级之名
    扩权,而调用方(和用户)都不会察觉结果集变大了。

    返回的 ``limit`` 是**实际生效的那个**而不是 ``max_rows`` —— 见
    :class:`LimitedSql`。
    """
    text = (sql or "").strip()
    if not text:
        return None
    from sqlglot import exp, parse_one

    try:
        tree = parse_one(text, read=dialect or None)
    except Exception:
        return None
    if tree is None or "limit" not in getattr(tree, "arg_types", {}):
        return None

    existing = _existing_limit(tree)
    if existing is not None and existing <= max_rows:
        return LimitedSql(sql, existing)  # 已经比预算更紧,不动
    try:
        tree.set("limit", exp.Limit(expression=exp.Literal.number(int(max_rows))))
        return LimitedSql(tree.sql(dialect=dialect or None), max_rows)
    except Exception:
        return None


def execution_evidence(
    est: CostEstimate,
    decision: BudgetDecision,
    budget: ExecutionBudget,
    *,
    limit_applied: int | None = None,
    data_as_of: str | None = None,
    as_of_basis: str = "",
    scanned_rows: int | None = None,
    terminated: str | None = None,
    kill: str = "",
) -> dict[str, Any]:
    """本次执行的**证据**,写进 state 随答案回给用户(设计 §6.2 / R1)。

    返 dict 而不是设计稿里的 ``ExecutionEvidence`` dataclass:state 每步都过一次
    checkpointer 序列化,未注册的 dataclass 会被**静默降级成 dict**(能力⑥ P3
    的原话,证据在 ``workflow/state.WorkflowState.principal`` 与契约的 wire 形
    式上)。嵌套 ``ExecutionBudget`` 的 dataclass 尤其危险 —— 它降级之后
    ``budget.max_rows`` 还能读出来,读者根本看不出类型已经掉了。同 ``authz_decision``
    的接法,直接以 wire 形状为唯一形状。

    ``degraded`` 与 ``limit_applied`` 必须一起看:``degraded=True`` 且
    ``limit_applied=None`` 是「没有依据、也没能加上边界」(I2 要求说出来,
    不要求做得到 —— 但要让人**看得出**)。

    ``budget`` 整份带上:阈值是配置,而配置会改,证据是历史。不带上它,事后
    无从判断这条查询是被哪一套阈值判的。

    ``as_of_basis`` 同样有三个状态,别把前两个混成一个(与 :func:`describe_cost`
    的 ``source=""`` / ``degraded`` 同一套纪律):

    * ``""`` —— **没查过**(没装画像)。说「无从判断」是撒谎:我们没判断过。
    * ``"unknown"`` —— 查过了,**无从判断**(I5)。这一句才是可以直接讲给用户的。
    * ``"last_modified" | "latest_partition"`` —— 有值,且**必须跟值一起展示**
      (R5:单看值会被读成「数据已更新到此刻」,口径才是它的含义)。

    ``scanned_rows`` 仍是 ``None``:适配器还没人报得出真实的扫描量。P3 曾记
    「P4 的 ``QueryTerminator`` 会补」——**P4 没有补,而且补不了**:那个模块
    暴露的是在飞查询的**身份**(给它一个能精确点名的 id),不是它的扫描计数;
    后者要么读 ``system.processes.read_rows``(CH),要么等 ``EXPLAIN ANALYZE``,
    是另一个观测点(设计 §17.8)。**不用返回行数冒充** —— 扫一亿行聚合出三行
    时两者差着七个数量级,拿返回行数去校准估算器会把估算一路调小,方向恰好
    是反的。

    ``terminated`` 与 ``kill`` 是**两件事**,别合并(P4):

    * ``terminated`` —— 查询**为什么结束**:``None``(正常跑完)或
      ``"timeout"``。设计 §6.2 的值域里还有 ``"budget_exceeded"``,但今天
      **没有生产者**:唯一的飞行中预算是墙钟 ``timeout_ms``,扫描量是**事前**
      估算,没有任何中途成本测量。按本方案一贯的纪律,没有生产者就不写进值域
      —— 造一个永远不出现的取值,等于给读者一个假的可能性空间。
    * ``kill`` —— 那条查询**有没有被主动终止**,三个状态分得开:
      ``""`` 没试过(没装终止器);``"kill_unsupported"`` 试过,这个数据源
      没有这个能力(静态事实);``"kill_sent"`` / ``"kill_failed"`` 发出去
      且驱动没报错 / 试了没成。
    """
    return {
        # I7:估算与实际分开记录
        "estimated_rows": est.estimated_rows,
        "estimated_bytes": est.estimated_bytes,
        "source": est.source,
        "degraded": est.degraded,
        "verdict": decision.verdict,
        "reason": decision.reason,
        "limit_applied": limit_applied,
        "scanned_rows": scanned_rows,
        "data_as_of": data_as_of,
        "as_of_basis": as_of_basis,
        "terminated": terminated,
        "kill": kill,
        "budget": asdict(budget),
    }


def _existing_limit(tree: Any) -> int | None:
    """顶层 LIMIT 的字面值;无 LIMIT / 非字面量(如占位符)→ None。"""
    limit = tree.args.get("limit")
    if limit is None:
        return None
    expr = limit.expression if hasattr(limit, "expression") else None
    try:
        return int(expr.name)
    except (TypeError, ValueError):
        return None


# ── 小工具 ────────────────────────────────────────────────


async def _maybe_await(value: Any) -> Any:
    """同步/异步依赖都接受 —— 生产传的是 async 的 ``connectors.explain``,
    测试与纯函数画像传同步的可调用对象。"""
    if inspect.isawaitable(value):
        return await value
    return value
