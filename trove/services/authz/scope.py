"""行/列授权的执行前改写 —— 确定性 SQL scope 引擎(零 LLM)。

授权的三层与各自的落点(设计决策 4 + 决策 3):

======================  =====================================================
数据源级(grants)       ``Authorizer`` A2 —— 能不能碰这个源
表级(declared tables)  ``Authorizer`` A3 —— 只能摸声明过的表
行级(row_grants)       **本模块**:表引用子查询重写 ``WHERE <谓词>``
列级(column_denials)   **本模块**:引用被禁列即拒绝(列名仍可见,值不许出)
======================  =====================================================

三个不可动摇的取向:

1. **fail-closed**:SQL 解析不了、列限定符解析不了、谓词解析不了 → 抛
   :class:`ScopeError`,绝不在「不知道有没有过滤」的状态下执行。
2. **只收窄**:``None`` = 未声明收窄(原样返回);声明的谓词是 AND 过滤,
   声明的被禁列是拒绝面。本模块没有任何放宽方向。
3. **未声明 = 逐字节不变**:没有行过滤且没有列拒绝时**不解析 SQL**,原样
   返回 —— 存量部署的执行链路上不多一次序列化往返,也不多一处行为差异。

行过滤为什么是**表引用子查询重写**而不是顶层 WHERE:顶层注入在
``UNION`` / CTE / 子查询里会把内层别名提到外层,产出无效 SQL;子查询重写
``FROM loans l`` → ``FROM (SELECT * FROM loans WHERE <各谓词 AND 起来>) l``
在所有这些形状下语义正确,且同一张表被多个别名引用时各自包裹。

**调用纪律:每条 SQL 每条执行路径只 apply 一次。** 生成的子查询里是
``SELECT *`` —— 对含被禁列的表,第二次 apply 会把这条自动补出的星号当成
用户越权投影而拒绝(一次合法查询因此变成 403)。这不是靠幂等标记绕开的
问题,而是「scope 是执行前的最后一步」的调用契约;候选执行、试跑、归因
hop 这些"先执行一次看看"的路径必须把**原 SQL** 交回主执行面,由
``execute_sql`` 做唯一一次改写(见 ``select.py`` / ``attribution.py`` 的接线)。

**明确不接的取数面(v1 非目标,均为管理员/系统通道,不是用户请求):**

- ``decision`` / ``scan`` 系统任务 —— 判定/扫描是 admin 配置的只读通道,
  只接受 admin owner(见 ``jobs/runner``),不套个人行/列 grant;
- ``kb init`` / profiling / 合成示例试跑 —— 管理员建模行为;
- 管理端语义预览 —— 管理员豁免(决策 4);
- ``metadata`` / 目录 / schema 浏览 —— 只返回**名字**,决策 3 允许。

新增用户可见取数入口**必须**复用本模块(统一入口
:func:`authorize_and_scope` 或同一道 ``apply_scope``);调用点清单由
``tests/services/authz/test_execute_sites_guard.py`` 的 AST 守卫枚举,
新入口不登记就红。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlglot import exp, parse_one

from trove.services.authz.enforcer import Authorizer, _base_name
from trove.services.authz.policy import Principal
from trove.services.semantic_layer.models import SemanticModel
from trove.services.semantic_layer.rls import (
    DEFAULT_DIALECT,
    declared_rls,
    physical_table,
    render_predicate,
)

__all__ = [
    "ScopeError",
    "ScopedSQL",
    "apply_scope",
    "authorize_and_scope",
    "denied_columns",
    "has_column_denials",
]

#: reason → ``[ERR:...]`` 错误码。与 ``enforcer._ERROR_TAGS`` 同一套词汇
#: (那边有 A1/A2/A3,这边多出 column);闭集由测试钉住。
_ERROR_TAGS = {
    "no_principal": "AUTHZ_NO_PRINCIPAL",
    "datasource": "AUTHZ_DATASOURCE",
    "table": "AUTHZ_TABLE",
    "column": "AUTHZ_COLUMN",
    "unresolved": "AUTHZ_UNRESOLVED",
}


class ScopeError(RuntimeError):
    """scope 判不了 / 判定不通过 —— 调用方**不得执行**这条 SQL。

    ``reason`` 是闭集(见 ``_ERROR_TAGS``);``tables`` / ``columns`` 是命中的
    授权面(审计与错误消息用,不含任何业务值)。
    """

    def __init__(
        self, reason: str, message: str = "", *,
        tables: tuple[str, ...] = (), columns: tuple[str, ...] = (),
    ) -> None:
        self.reason = reason if reason in _ERROR_TAGS else "unresolved"
        self.tables = tuple(sorted(tables))
        self.columns = tuple(sorted(columns))
        self.detail = message
        super().__init__(self.render())

    def error_tag(self) -> str:
        return _ERROR_TAGS[self.reason]

    def render(self) -> str:
        tag = self.error_tag()
        detail = f" {self.detail}" if self.detail else ""
        return f"[ERR:{tag}] {self.user_message()}{detail}".strip()

    def user_message(self) -> str:
        """用户可见的拒绝文案(与 ``execute_sql._authz_message`` 同一语气/语言)。

        列名会点名(决策 3:列**名**可见,拒绝要说清被挡的是哪一列);表
        名只在 ``table`` 档点名(既有 A3 文案的先例)。
        """
        if self.reason == "column":
            cols = ", ".join(self.columns) or "restricted columns"
            return (
                f"This query references columns this identity is not authorized "
                f"to read: {cols}. The query was not executed."
            )
        if self.reason == "table":
            tables = ", ".join(self.tables) or "restricted tables"
            return (
                f"This identity is not authorized to query: {tables}. "
                "The query was not executed."
            )
        if self.reason == "datasource":
            return (
                "This identity is not authorized for the requested datasource, "
                "so the query was not executed."
            )
        if self.reason == "no_principal":
            return (
                "This run carries no authenticated identity, so the query was "
                "not executed."
            )
        return (
            "Row/column authorization could not be confirmed for this query, "
            "so it was not executed."
        )


@dataclass(frozen=True)
class ScopedSQL:
    """一次 scope 的产物。

    ``row_filters`` 是被包裹的表名排序元组(审计/答案注记);``rewritten``
    为假时 ``sql`` 与入参**逐字节相同**。``blocked_columns`` 只在拒绝路径
    的意义上存在(成功恒为空)—— 保留字段是给调用方收集拒绝证据用的。
    """

    sql: str
    row_filters: tuple[str, ...] = ()
    blocked_columns: tuple[str, ...] = ()
    rewritten: bool = False


# ── 公开入口 ──────────────────────────────────────────────────


def authorize_and_scope(
    sql: str,
    *,
    authorizer: Authorizer | None,
    principal: Principal | None,
    model: SemanticModel | None,
    datasource: str,
    dialect: str = "",
    default: str | None = None,
    include_declared_rls: bool = True,
) -> ScopedSQL:
    """A1–A3 判定 + 行/列改写 —— 用户可见取数路径的统一入口。

    ``authorizer is None``(嵌入部署没有鉴权面)时跳过 A1–A3,但**仍做
    scope**:主体上带着 grant 就必须生效,不能因为少装了个组件而放宽。
    """
    if authorizer is not None:
        decision = authorizer.check(
            principal, datasource=datasource, sql=sql, default=default,
            dialect=dialect,
        )
        if not decision.allowed:
            raise ScopeError(
                decision.reason,
                tables=tuple(decision.narrowed_tables),
            )
    return apply_scope(
        sql, model=model, principal=principal, datasource=datasource,
        dialect=dialect, include_declared_rls=include_declared_rls,
    )


def apply_scope(
    sql: str,
    *,
    model: SemanticModel | None,
    principal: Principal | None,
    datasource: str,
    dialect: str = "",
    include_declared_rls: bool = True,
) -> ScopedSQL:
    """对一条待执行 SQL 施加主体行/列授权(及可选的声明 RLS)。

    Args:
        sql: 待执行语句(单条;多语句在被调用方拒绝)。
        model: 语义模型(声明 RLS 的来源);``None`` = 只套主体 grant
            (grant 与 SQL 都是物理表名,不需要语义层)。
        principal: 主体;``None`` → :class:`ScopeError` ``no_principal``。
        datasource: 目标数据源名(grant 按源分桶)。
        dialect: sqlglot 方言;空 = 默认。
        include_declared_rls: 调用方声明**这条 SQL 里是否已经注入过**声明层
            ``row_filter``。编译路径与快径已在 SQL 里注入(传 ``False``,只
            叠加主体 grant);生成/工具/候选路径从未注入过(传 ``True``)。

    Raises:
        ScopeError: 没有主体、SQL/谓词/别名解析不了、或引用了被禁列。
    """
    if principal is None:
        raise ScopeError("no_principal")

    filters = _filters_for(
        principal, datasource, model,
        include_declared_rls=include_declared_rls,
    )
    denials = _denials_for(principal, datasource)

    # 未声明 = 逐字节不变(Object identity 上的「不碰」,不是等价重写)
    if not filters and not denials:
        return ScopedSQL(sql=sql)

    if not (sql or "").strip():
        return ScopedSQL(sql=sql)

    d = dialect or DEFAULT_DIALECT
    try:
        tree = parse_one(sql, read=d)
    except Exception as exc:
        raise ScopeError("unresolved", f"SQL 无法解析: {exc}") from exc
    if tree is None:
        raise ScopeError("unresolved", "SQL 无法解析")

    # 先判列:拒绝路径不产生任何改写产物(不给调用方半成品)
    if denials:
        _check_columns(tree, denials)

    if not filters:
        return ScopedSQL(sql=sql)

    wrapped = _rewrite_tables(tree, filters, d)
    if not wrapped:
        # 声明了过滤但整条 SQL 没引用那些表(如纯 CTE 扫描/常量查询)
        return ScopedSQL(sql=sql)
    return ScopedSQL(
        sql=tree.sql(dialect=d),
        row_filters=tuple(sorted(wrapped)),
        rewritten=True,
    )


def has_column_denials(principal: Principal | None, datasource: str) -> bool:
    """该数据源下主体是否声明了列拒绝 —— **证据/注记用,不参与判定**。

    执行成功时 ``data_scope`` 要如实回答「这一问有没有列策略在生效」:
    「没有列策略」与「声明了但这条 SQL 恰好没碰」对审阅者是两件事。
    """
    if principal is None:
        return False
    return bool(_slice(principal.column_denials, datasource))


def denied_columns(
    principal: Principal | None, datasource: str, table: str,
) -> frozenset[str]:
    """该主体在 ``table`` 上被禁的列名(小写)—— **扫描面决策用**。

    ``search_values`` 需要在**生成 SQL 之前**决定扫哪些列:被禁列既不能
    显式查询,也不能混进「未指定列」的全列扫描(那等于换一条路径把值读
    出来)。判定口径与 :func:`apply_scope` 的列拒绝同源(同一份
    ``_denials_for``),两处不会漂移;真正的执行门仍是 ``apply_scope``。
    """
    if principal is None:
        return frozenset()
    return _denials_for(principal, datasource).get(_base_name(table), frozenset())


# ── 授权面解析 ────────────────────────────────────────────────


def _slice(mapping: dict[str, Any] | None, datasource: str) -> dict[str, Any]:
    """按数据源取一个分桶;精确名优先,大小写兜底一次。没有 → 空。"""
    if not mapping:
        return {}
    if datasource in mapping:
        return dict(mapping[datasource] or {})
    wanted = str(datasource or "").strip().lower()
    for name, value in mapping.items():
        if str(name).strip().lower() == wanted:
            return dict(value or {})
    return {}


def _filters_for(
    principal: Principal, datasource: str, model: SemanticModel | None, *,
    include_declared_rls: bool,
) -> dict[str, list[str]]:
    """``{物理表名小写: [谓词, ...]}`` —— 声明 RLS(可选)+ 主体行授权。

    两路来源合进同一个列表:同一张表的两个谓词会在**同一个子查询**里
    ``AND`` 起来,而不是包两层派生表。顺序固定(声明在前、grant 在后且按
    表名排序),同一份输入两次运行产出逐字节相同的 SQL。
    """
    out: dict[str, list[str]] = {}
    if include_declared_rls:
        for dataset in declared_rls(model):
            table = physical_table(dataset)
            pred = (dataset.row_filter or "").strip()
            if table and pred:
                out.setdefault(table, []).append(pred)
    grant_tables = _slice(principal.row_grants, datasource)
    for table in sorted(grant_tables):
        pred = str(grant_tables[table] or "").strip()
        base = _base_name(table)
        if base and pred:
            out.setdefault(base, []).append(pred)
    return out


def _denials_for(
    principal: Principal, datasource: str,
) -> dict[str, frozenset[str]]:
    """``{物理表名小写: frozenset(列名小写)}``;admin 的 ``None`` → 空。"""
    out: dict[str, frozenset[str]] = {}
    for table, columns in _slice(principal.column_denials, datasource).items():
        base = _base_name(table)
        cols = frozenset(str(c).strip().lower() for c in columns or ()
                         if str(c).strip())
        if base and cols:
            out[base] = cols
    return out


# ── 列拒绝 ────────────────────────────────────────────────────


def _check_columns(tree: exp.Expression, denials: dict[str, frozenset[str]]) -> None:
    """引用被禁列 → :class:`ScopeError`;解析不了 → ``unresolved``。

    覆盖面(决策 3:列**名**可见,值不可取):
    - 投影 / WHERE / ORDER BY / GROUP BY / HAVING / 函数参数里的一切列引用;
    - ``SELECT *`` / ``t.*`` 对含被禁列的表 —— 星号会带出全部值,按引用
      全部列处理(**拒绝**;逐个列展开需要 catalog,不在安全路径里做);
    - ``COUNT(*)`` **允许** —— 星号不在直接投影位置,计数不泄露任何值。
    """
    for select in tree.find_all(exp.Select):
        _check_star_projection(select, denials)

    for col in tree.find_all(exp.Column):
        if isinstance(col.this, exp.Star):
            # 限定星号 t.* —— 解析 t 后按「表含任何被禁列」拒绝
            table = _resolve_qualifier(col, col.table)
            if table is None:
                raise ScopeError(
                    "unresolved", f"无法解析列限定符 {col.table!r}")
            if table.kind == "table" and denials.get(table.name):
                raise ScopeError(
                    "column", f"星号投影会带出 {table.name} 的全部列",
                    tables=(table.name,),
                    columns=tuple(f"{table.name}.{c}" for c in denials[table.name]),
                )
            continue

        name = (col.name or "").strip().lower()
        if not name:
            continue
        if col.table:
            table = _resolve_qualifier(col, col.table)
            if table is None:
                raise ScopeError(
                    "unresolved", f"无法解析列限定符 {col.table!r}")
            if table.kind == "table" and name in denials.get(table.name, ()):
                raise ScopeError(
                    "column",
                    f"{table.name}.{name} 是受限列",
                    tables=(table.name,),
                    columns=(f"{table.name}.{name}",),
                )
            continue

        # 未限定列:沿作用域链向外找所有能承载它的物理表。
        # 任一命中被禁列即拒绝 —— 歧义方向从严(宁可拒绝,不猜指向)。
        for table in _unqualified_candidates(col):
            if table.kind == "table" and name in denials.get(table.name, ()):
                raise ScopeError(
                    "column",
                    f"{table.name}.{name} 是受限列",
                    tables=(table.name,),
                    columns=(f"{table.name}.{name}",),
                )


def _check_star_projection(
    select: exp.Select, denials: dict[str, frozenset[str]],
) -> None:
    """``SELECT *``(不带表限定)的直接投影 —— 该 SELECT 自己的物理表有被禁列即拒绝。"""
    if not any(isinstance(e, exp.Star) for e in select.expressions):
        return
    for binding in _scope_bindings(select).values():
        if binding.kind == "table" and denials.get(binding.name):
            raise ScopeError(
                "column", f"SELECT * 会带出 {binding.name} 的全部列",
                tables=(binding.name,),
                columns=tuple(f"{binding.name}.{c}" for c in denials[binding.name]),
            )


@dataclass(frozen=True)
class _Binding:
    """一个 FROM/JOIN 目标在列解析里的身份。

    ``kind="table"`` = 物理表(``name`` 是裸表名,小写);
    ``kind="derived"`` = 子查询 / CTE / 其他派生源 —— 值已经被内层检查过,
    外层引用不再套用表级拒绝(否则会误伤 ``SELECT q.name FROM (SELECT name
    FROM client) q``:内层只取了 name,外层并没有越权)。
    """

    kind: str
    name: str = ""


def _resolve_qualifier(col: exp.Column, qualifier: str) -> _Binding | None:
    """列限定符 → 绑定;沿**作用域栈**逐层找(相关子查询能解析到外层)。"""
    key = str(qualifier or "").strip().lower()
    if not key:
        return None
    scope = col.find_ancestor(exp.Select)
    while scope is not None:
        binding = _scope_bindings(scope).get(key)
        if binding is not None:
            return binding
        scope = scope.find_ancestor(exp.Select)
    return None


def _unqualified_candidates(col: exp.Column) -> list[_Binding]:
    """未限定列可能归属的物理表(由内向外每一层的 FROM/JOIN)。"""
    out: list[_Binding] = []
    scope = col.find_ancestor(exp.Select)
    while scope is not None:
        out.extend(
            b for b in _scope_bindings(scope).values() if b.kind == "table")
        scope = scope.find_ancestor(exp.Select)
    # 同一张表在多层里出现只留一份(bindings 是 dict,但外层可能重复)
    seen: set[str] = set()
    unique: list[_Binding] = []
    for binding in out:
        if binding.name not in seen:
            seen.add(binding.name)
            unique.append(binding)
    return unique


def _scope_bindings(select: exp.Select) -> dict[str, _Binding]:
    """一个 SELECT 自己的 FROM/JOIN 名字 → 绑定(别名与裸表名都登记)。

    别名与裸表名都登记是**从严**的一侧:SQL 里出现 ``loans.x`` 而表被
    ``FROM loans l`` 别名化时,多数方言会报「没有 loans」—— 在报错之前,
    若 loans 有列拒绝,我们仍按拒绝处理,方向不出错。
    """
    targets: list[exp.Expression] = []
    from_ = select.args.get("from_")
    if from_ is not None and from_.this is not None:
        targets.append(from_.this)
    for join in select.args.get("joins") or ():
        if join.this is not None:
            targets.append(join.this)

    ctes = _visible_cte_names(select)
    bindings: dict[str, _Binding] = {}
    for target in targets:
        alias = str(getattr(target, "alias", "") or "").strip().lower()
        if isinstance(target, exp.Table):
            base = _base_name(target.name)
            if not base:
                continue
            if not target.db and base in ctes:
                binding = _Binding("derived")
            else:
                binding = _Binding("table", base)
            if alias:
                bindings.setdefault(alias, binding)
            bindings.setdefault(base, binding)
        else:
            # 子查询 / VALUES / UNNEST / LATERAL —— 值面在内层已判过
            if alias:
                bindings.setdefault(alias, _Binding("derived"))
    return bindings


def _visible_cte_names(node: exp.Expression) -> set[str]:
    """``node`` 可见的 CTE 别名(它所有祖先上的 WITH 声明)。

    只认祖先作用域里的 CTE —— 兄弟分支里定义的同名 CTE 不该把一条真表
    引用当成别名(那会跳过行过滤,方向就是放宽)。CTE 体里引用的更早
    CTE 覆盖在同一祖先的 WITH 里,同一份集合即可。
    """
    names: set[str] = set()
    current: exp.Expression | None = node
    while current is not None:
        # sqlglot 30 的键是 ``with_``;旧版本是 ``with`` —— 两个都认,
        # 漏认的后果是「CTE 引用被当成物理表」→ 误拒/误过滤(方向虽严,
        # 但合法查询会被打挂,不是可接受的行为差异)
        args = current.args if isinstance(current.args, dict) else {}
        with_ = args.get("with_") or args.get("with")
        if with_ is not None:
            for cte in with_.expressions:
                alias = str(getattr(cte, "alias", "") or "").strip().lower()
                if alias:
                    names.add(alias)
        current = current.parent
    return names


# ── 行过滤重写 ────────────────────────────────────────────────


def _rewrite_tables(
    tree: exp.Expression, filters: dict[str, list[str]], dialect: str,
) -> set[str]:
    """把命中的物理表引用替换成带谓词的派生表;返回被包裹的表名集合。

    ``FROM loans l`` → ``FROM (SELECT * FROM loans WHERE (p1) AND (p2)) l``。
    同一张表被多个别名引用时**各自包裹**(每个引用点都要带上过滤)。
    """
    wrapped: set[str] = set()
    # find_all 的遍历结果先快照:替换会改动父子关系
    for table in list(tree.find_all(exp.Table)):
        if not table.name:
            continue
        base = _base_name(table.name)
        if not table.db and base in _visible_cte_names(table):
            continue                      # CTE 引用不是物理表
        predicates = filters.get(base)
        if not predicates:
            continue

        condition = _combined_predicate(predicates, dialect, base)
        alias = str(table.alias or "").strip() or base
        inner = table.copy()
        inner.set("alias", None)
        subquery = exp.Subquery(
            this=exp.Select(
                expressions=[exp.Star()],
                from_=exp.From(this=inner),
                where=exp.Where(this=condition),
            ),
            alias=exp.TableAlias(this=exp.to_identifier(alias)),
        )
        table.replace(subquery)
        wrapped.add(base)
    return wrapped


def _combined_predicate(
    predicates: list[str], dialect: str, table: str,
) -> exp.Expression:
    """多个谓词 → AND 树;任一解析不了 → ``unresolved``(绝不少一个条件)。

    谓词先经 ``rls.render_predicate`` 做裸列限定(与声明 RLS 同一份实现),
    再解析成 AST 嵌进子查询。含子查询的谓词拒绝:它的裸列归属无法在
    一个作用域里确定,而 grant 谓词在写入口就已限定为「列属于该表」。
    """
    conditions: list[exp.Expression] = []
    for raw in predicates:
        text = render_predicate(raw, dialect, table)
        if not text:
            continue
        try:
            node = parse_one(text, read=dialect)
        except Exception as exc:
            raise ScopeError(
                "unresolved",
                f"{table} 的行过滤谓词无法解析: {exc}",
                tables=(table,),
            ) from exc
        if node is None:
            raise ScopeError(
                "unresolved", f"{table} 的行过滤谓词无法解析",
                tables=(table,))
        if node.find(exp.Select) is not None:
            raise ScopeError(
                "unresolved", f"{table} 的行过滤谓词不允许包含子查询",
                tables=(table,))
        conditions.append(node)
    condition = conditions[0]
    for node in conditions[1:]:
        condition = exp.And(this=condition, expression=node)
    return condition
