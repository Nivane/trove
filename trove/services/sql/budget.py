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
    """
    return {
        # I7:估算与实际分开记录(实际由 P2/P4 补 ``scanned_rows``)
        "estimated_rows": est.estimated_rows,
        "estimated_bytes": est.estimated_bytes,
        "source": est.source,
        "degraded": est.degraded,
        "verdict": decision.verdict,
        "reason": decision.reason,
        "limit_applied": limit_applied,
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
