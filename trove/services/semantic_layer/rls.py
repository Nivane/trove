"""声明层行级安全(RLS)的注入实现 —— 编译路径与快径共用的**唯一**一份。

为什么单独成模块:``row_filter`` 原先只在 ``SemanticCompiler`` 内部注入,而
``fast_match`` 直接产出模板 SQL、**根本不过编译器**(见 ``fast_match`` 模块
docstring:"跳过 query_sketch + gen_sql")。结果是声明了 row_filter 的数据集
走快径时统计的是**全表** —— 声明层授权在快径上失效。

把渲染规则提取成纯函数,两条路径共用同一份实现,避免"同一策略两份副本"
(该模式在鉴权侧已经发生过一次,见 authz 设计文档 §2.2 G1)。

注入失败的语义与只读门相反(见 §10):只读门 fail-open 是因为数据库侧只读
角色是硬边界、有兜底;RLS 注入失败**没有任何东西兜底**,所以本模块
:class:`RLSInjectionError` 是 fail-closed —— 调用方应据此**放弃快径、回落到
编译路径**(编译路径会正常注入 RLS),而不是带着未过滤的 SQL 继续执行。
"""

from __future__ import annotations

import re

from sqlglot import exp, parse_one

from trove.services.semantic_layer.models import SemanticDataset, SemanticModel

DEFAULT_DIALECT = "sqlite"

_WS_RE = re.compile(r"\s+")


class RLSInjectionError(RuntimeError):
    """无法确定 RLS 是否已生效 —— 调用方必须放弃当前路径(不得静默放行)。"""


def physical_table(dataset: SemanticDataset) -> str:
    """数据集 → 物理表名(去 schema 前缀,小写);source 空回退数据集名。"""
    src = (dataset.source or "").strip()
    if not src:
        src = dataset.name
    return src.rsplit(".", 1)[-1].strip().lower()


def declared_rls(model: SemanticModel | None) -> list[SemanticDataset]:
    """模型里声明了 row_filter 的数据集(空 = 该模型完全无 RLS)。"""
    if model is None:
        return []
    return [d for d in model.datasets if (d.row_filter or "").strip()]


def render_row_filter(
    dataset: SemanticDataset | None, dialect: str, qualifier: str,
) -> str | None:
    """数据集 row_filter → 注入用谓词(裸列限定到 ``qualifier``);无声明 → None。

    解析失败**原样注入**而不静默丢弃:声明层 lint 已在写盘前拦截,这里若仍
    遇到坏谓词,宁可由执行期报语法错,也不能**少一个安全条件**。
    """
    rf = (dataset.row_filter if dataset is not None else "").strip()
    if not rf:
        return None
    d = dialect or DEFAULT_DIALECT
    try:
        tree = parse_one(rf, read=d)
    except Exception:
        return f"({rf})"
    if tree is None:
        return f"({rf})"
    for col in tree.find_all(exp.Column):
        if not col.table:
            col.set("table", exp.to_identifier(qualifier, quoted=False))
    return f"({tree.sql(dialect=d)})"


def inject_row_filters(
    sql: str, model: SemanticModel | None, dialect: str = "",
) -> str:
    """把 ``model`` 里声明的 row_filter 注入 ``sql`` 的顶层 WHERE。

    用于**未经编译器**的 SQL(快径模板、外部直执行)。幂等:已含该谓词的
    SQL 原样返回 —— 编译路径产出的 SQL 已经注入过,重复注入虽语义等价
    (``A AND A``),却会让编译保真校验的契约比对失配。

    Raises:
        RLSInjectionError: SQL 无法解析 —— 此时无法确认过滤是否生效,调用方
            应回落到编译路径。SQL 引用的数据集未声明 row_filter 时**不是**
            错误(无可注入、无可确认),原样返回。
    """
    if not (sql or "").strip():
        return sql
    declared = declared_rls(model)
    if not declared:
        return sql  # 无 RLS 声明 —— 不付解析成本(快径的常态)

    d = dialect or DEFAULT_DIALECT
    try:
        tree = parse_one(sql, read=d)
    except Exception as exc:
        raise RLSInjectionError(f"SQL 无法解析,拒绝放行未确认 RLS 的语句: {exc}") from exc
    if tree is None:
        raise RLSInjectionError("SQL 无法解析,拒绝放行未确认 RLS 的语句")

    preds: list[str] = []
    seen_tables: set[str] = set()
    for tbl in tree.find_all(exp.Table):
        ident = (tbl.name or "").strip()
        if not ident or ident.lower() in seen_tables:
            continue
        seen_tables.add(ident.lower())
        ds = _dataset_for(model, ident)
        if ds is None:
            continue
        pred = render_row_filter(ds, d, tbl.alias or ident)
        if pred and not _already_present(sql, pred):
            preds.append(pred)

    # 空 preds 不是错误:SQL 引用的数据集**本来就没声明 row_filter** 时无从
    # 注入,也无可确认 —— 把它当失败会让「模型里只要有一个数据集声明了 RLS,
    # 其余数据集的查询就全部无法走快径」。解析失败才是唯一 fail-closed 场景,
    # 上面已拦。
    if not preds:
        return sql

    cond = parse_one(" AND ".join(preds), read=d)
    where = tree.args.get("where")
    if where is not None and where.this is not None:
        tree.set("where", exp.Where(this=exp.And(this=where.this, expression=cond)))
    else:
        tree.set("where", exp.Where(this=cond))
    return tree.sql(dialect=d)


def _dataset_for(model: SemanticModel, table: str) -> SemanticDataset | None:
    """SQL 里的表名 → 数据集。先按数据集名,再按物理表名(去 schema)。"""
    t = table.strip().lower()
    if not t:
        return None
    for ds in model.datasets:
        if ds.name.strip().lower() == t:
            return ds
    for ds in model.datasets:
        if physical_table(ds) == t:
            return ds
    return None


def _already_present(sql: str, pred: str) -> bool:
    """谓词是否已在 SQL 文本中(归一空白 + 小写后的子串比对)。

    文本级而非 AST 级:作用只是避免重复注入,判错(漏判)的后果是
    ``A AND A``,语义等价。宁可重复也不误判为"已注入"而漏掉过滤。
    """
    norm = lambda s: _WS_RE.sub(" ", s).strip().lower()  # noqa: E731
    return norm(pred) in norm(sql)
