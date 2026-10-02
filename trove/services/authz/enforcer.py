"""执行层强制点 —— 执行 SQL 之前的**最后一米**(设计 §5.3 / G2)。

为什么需要它。鉴权在存量实现里是「路由的装饰器」:``Depends(require_datasource)``
的自然语义是「这个**端点**需要授权」。于是只要有一条不经路由的执行路径 ——
图内的 ``fast_match``、MCP 工具、API 直执行、job payload —— 约束就断了。
设计 §2.3 把这叫「把鉴权当成了路由的装饰器,而不是执行的前置条件」。

本模块把判定挪到 SQL 落库之前:无论 SQL 从哪来、经过谁,都要在这里过一次。

三道判定
--------

====  ==========================================  ==========================
A1    principal 存在                                I2 / I7:缺失 → 拒绝
A2    ``principal.allows_datasource(datasource)``  数据源级授权
A3    SQL 触及的表 ⊆ 语义层声明过的表               表级(**默认 warn**)
====  ==========================================  ==========================

A3 的口径是 2026-09-29 定的:仓库的 grants 只到数据源级(``user_datasources``
只有 user_id + datasource),没有数据集级授权源。所以 A3 挡的不是「另一个用户的
数据集」,而是**声明之外的表** —— 绕开语义层直接摸物理表。编译路径与快径产出的
SQL 只引用声明过的表,因此不会被误伤;只有直执行会命中。首次上线默认 ``warn``
(记 metric + 日志),理由见设计 §8.2:直接 enforce 会让存量部署大面积 403。

失败方向(设计 §8.1 判据:**有兜底的方向可以宽,没兜底的方向必须严**)
------------------------------------------------------------------

- **A1 缺主体 → 拒绝**(closed)。没有任何东西在 SQL 落库前检查「谁在问」。
- **A2 数据源不在 grants → 拒绝**(closed)。沿用既有语义。
- **A3 命中声明之外的表 → enforce 拒 / warn 放行 + 记录**。可配置,见 §8.2。
- **A3 无基准(语义层没接 / 声明为空)→ 跳过**,不拒。**基准不存在 != 判定不
  通过** —— 判「表 ⊆ ∅」会把没上语义层的部署一次全拒掉,那是把安全改进做成
  事故。A1/A2 不受影响,仍然生效。
- **SQL 解析不了 → enforce 拒 / warn 放行**。与 §10 的 RLS 注入失败同向:
  解析不了就无法确认触及了什么,而没有下层兜底。注意这与只读门的 fail-open
  不矛盾 —— 只读门有数据库侧只读角色兜底,这里没有。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from trove.services.authz.policy import Principal

__all__ = ["AuthzDecision", "Authorizer", "referenced_tables"]

#: 数据源名 → 该数据源在语义层里声明过的表名集合。
#: 返回 ``None`` 或空集 = **没有基准**(未接语义层),A3 跳过。
DeclaredTables = Callable[[str | None], "set[str] | None"]

MODES = ("warn", "enforce")

#: reason → 错误码(设计 §10)。映射只有一份,免得各调用点各拼一遍。
_ERROR_TAGS = {
    "no_principal": "AUTHZ_NO_PRINCIPAL",
    "datasource": "AUTHZ_DATASOURCE",
    "table": "AUTHZ_TABLE",
    "unresolved": "AUTHZ_UNRESOLVED",
}


@dataclass(frozen=True)
class AuthzDecision:
    """一次执行前判定的结论。

    ``narrowed_tables`` = **被 A3 挡住的表**(声明之外的那些)。warn 期这个字段
    是主要产物:§8.2 要「先跑一周收集哪些表会被拒」,而 warn 的判定
    ``allowed=True`` 单看布尔读不出任何东西。设计 §7.1 只给了字段名没给语义,
    此处按 warn 期的实际用途定死。
    """

    allowed: bool
    reason: str = ""
    narrowed_tables: list[str] = field(default_factory=list)

    def error_tag(self) -> str:
        """``[ERR:...]`` 里的错误码;放行时为空串。"""
        if self.allowed:
            return ""
        return _ERROR_TAGS.get(self.reason, "AUTHZ_DENIED")


class Authorizer:
    """执行前的最后一道(纯同步判定,不碰 I/O)。

    Args:
        declared_tables: 数据源 → 语义层声明过的表名。``None`` = 不判 A3
            (只做 A1/A2),留给没接语义层的部署。
        mode: ``enforce``(默认)或 ``warn``(§8.2 观察期档,回退阀)。

    Raises:
        ValueError: ``mode`` 不认识。**刻意不静默降级成 warn** —— 配置写错时
            「以为开了 enforce 其实没开」比直接起不来危险得多。
    """

    def __init__(
        self, declared_tables: DeclaredTables | None = None, *, mode: str = "enforce",
    ) -> None:
        normalized = str(mode or "").strip().lower()
        if normalized not in MODES:
            raise ValueError(
                f"authz.table_enforcement 只能是 {MODES} 之一,收到 {mode!r}"
            )
        self.mode = normalized
        self._declared = declared_tables

    def check(
        self,
        principal: Principal | None,
        *,
        datasource: str,
        tables: Iterable[str] | None = None,
        sql: str = "",
        default: str | None = None,
        dialect: str = "",
    ) -> AuthzDecision:
        """执行前判定。

        Args:
            principal: 提问者的主体;``None`` = 没有主体 → 拒绝(I2/I7)。
            datasource: 目标数据源(空 = 注册表默认)。
            tables: SQL 触及的表。``None`` = 调用方没解析,由 ``sql`` 回填 ——
                让「谁解析表名」也只有一份实现。
            sql: 待执行语句,供回填与审计。
            default: 注册表默认源名(空 grants 时唯一放行的那个)。
            dialect: 解析 ``sql`` 用的方言;空 = sqlglot 默认。
        """
        # ── A1:主体存在(I2 / I7)────────────────────────────────
        if principal is None:
            return AuthzDecision(False, "no_principal")

        # ── A2:数据源级授权 ──────────────────────────────────────
        target = (datasource or "").strip() or (default or "")
        if not principal.allows_datasource(target, default):
            return AuthzDecision(False, "datasource")

        # ── A3:表级(语义层白名单)───────────────────────────────
        return self._check_tables(tables=tables, sql=sql, datasource=target, dialect=dialect)

    def _check_tables(
        self,
        *,
        tables: Iterable[str] | None,
        sql: str,
        datasource: str | None,
        dialect: str,
    ) -> AuthzDecision:
        declared = self._declared(datasource) if self._declared is not None else None
        if not declared:
            # 基准不存在 != 判定不通过(模块 docstring 的第三类「无从判定」)
            return AuthzDecision(True)
        declared_norm = {_base_name(t) for t in declared}

        if tables is None:
            if not (sql or "").strip():
                return AuthzDecision(True)  # 既无表清单也无 SQL —— 无从判定
            resolved = referenced_tables(sql, dialect)
            if resolved is None:
                # 解析不了 = 无法确认触及了什么。没有下层兜底的方向必须严。
                if self.mode == "enforce":
                    return AuthzDecision(False, "unresolved")
                return AuthzDecision(True)
            tables = resolved

        blocked = sorted({_base_name(t) for t in tables if t} - declared_norm)
        if blocked and self.mode == "enforce":
            return AuthzDecision(False, "table", blocked)
        # 放行时也把被挡的表带出来 —— warn 期的可观测性全靠这个字段
        return AuthzDecision(True, "", blocked)


def referenced_tables(sql: str, dialect: str = "") -> set[str] | None:
    """SQL 触及的表名集合(归一为小写裸表名);**解析不了返回 ``None``**。

    ``None`` 与空集是两个语义(同 ``contract_from_wire`` 的取舍):空集是
    「解析成功且没有表」(``SELECT 1``),``None`` 是「不知道」。把它们混成一个
    值,A3 就没法在「无从判定」与「判定通过」之间分开 —— 而那正是本模块最需要
    分开的一处。

    取的是 FROM/JOIN 的目标表,不是列限定符:列限定符可能指向 CTE 别名,
    当成表名会误报。子查询/联表里的表都会被 ``find_all`` 收进来。

    **CTE 别名同样不是表**:``FROM <cte>`` 在 sqlglot 里也是 ``exp.Table``,
    不排除就会把别名送进 A3,在有 CTE 的库上造成与权限无关的误拒。CTE 体里
    的真表仍会被收进来(排除的是别名,不是内容)。与
    ``services/sql/guard.py`` 的判据同源 —— 两处对「SQL 触及哪些表」必须一致。
    """
    from sqlglot import exp, parse_one

    if not (sql or "").strip():
        return set()
    try:
        tree = parse_one(sql, read=dialect or None)
    except Exception:
        return None
    if tree is None:
        return None
    cte_names = {c.alias.lower() for c in tree.find_all(exp.CTE) if c.alias}
    return {
        _base_name(t.name)
        for t in tree.find_all(exp.Table)
        if t.name and _base_name(t.name) not in cte_names
    }


def _base_name(table: str) -> str:
    """表名归一小写 + 去 schema 前缀 —— 两侧用同一份,比较才有意义。"""
    return str(table or "").strip().rsplit(".", 1)[-1].lower()
