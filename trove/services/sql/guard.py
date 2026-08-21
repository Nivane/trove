"""AST 级只读 SQL 防火墙 (SqlFirewall)。

防御定位:应用层纵深防御,不是安全边界——真正的边界在数据库层
(只读角色 / 列级 grant / 行限制)。本模块拦截:
  - 写语句(DML/DDL/SET/CALL/COPY/MERGE/REPLACE INTO/多语句)
  - data-modifying CTE(顶层伪装成 SELECT,树内藏 DML)
  - MySQL SELECT ... INTO OUTFILE/DUMPFILE(写文件面)
  - 危险函数(SLEEP/BENCHMARK/LOAD_FILE/pg_sleep 等资源/信息泄露面)
  - 元数据表侦察(sqlite_master / information_schema / pg_catalog / pg_*)
  - 可选表名 allowlist(schema 快照可用时)

实现要点:
  - 基于 sqlglot AST(RAISE 解析),天然免疫关键词正则的绕过
    (注释拆分 DEL/**/ETE、可执行注释 /*!50000 UPDATE ... */ 等)。
  - 方言回退:默认方言解析失败时回退 mysql(内部 SQL 的反引号标识符)。
  - 纯函数,零依赖(复用项目已有的 sqlglot),可独立单测。
"""

from __future__ import annotations

from trove.core.logging import get_logger

logger = get_logger(__name__)

# 元数据/系统表:无论是否在 allowlist 中一律拒绝(侦察面)
META_TABLES = frozenset({
    "sqlite_master",
    "sqlite_sequence",
    "sqlite_temp_master",
    "sqlite_temp_sequence",
})
META_CATALOGS = frozenset({
    "information_schema",
    "pg_catalog",
    "pg_toast",
})
# 危险函数:资源耗尽 / 服务端文件读取面
DANGEROUS_FUNCS = frozenset({
    "SLEEP",
    "BENCHMARK",
    "LOAD_FILE",
    "PG_SLEEP",
    "PG_READ_FILE",
    "PG_READ_BINARY_FILE",
})
# SELECT ... INTO 的写文件形态(MySQL)
INTO_WRITE_KINDS = frozenset({"OUTFILE", "DUMPFILE"})


def check_readonly(
    sql: str,
    dialect: str = "",
    allowed_tables: set[str] | None = None,
) -> tuple[bool, list[str]]:
    """检查 SQL 是否只读且合规。

    Args:
        sql: 待检查 SQL。
        dialect: 目标方言(sqlglot;反引号标识符自动回退 mysql)。
        allowed_tables: 允许引用的业务表集合(小写归一)。None 时只拒绝
            元数据表,业务表全放行;给定集合时业务表必须 ∈ 集合。

    Returns:
        (ok, reasons):ok=False 时 reasons 给出全部拒绝原因(深度防御,
        一次报全,方便模型/用户一次性修正)。
    """
    reasons: list[str] = []
    if not sql or not sql.strip():
        return False, ["SQL is empty"]

    parsed = _parse(sql, dialect)
    if parsed is None:
        return False, ["SQL could not be parsed; treat as a syntax error, not a permission denial"]

    try:
        import sqlglot
        from sqlglot import exp
    except ImportError:
        logger.warning("sqlglot not available; read-only guard disabled")
        return True, []

    if not isinstance(parsed, exp.Query):
        reasons.append(
            f"only SELECT queries are allowed (rejected: {type(parsed).__name__})"
        )
        return False, reasons

    # 整树扫描 DML/DDL:data-modifying CTE(WITH x AS (DELETE ...) SELECT)
    # 顶层是 Select,但树内藏写操作——只查顶层会放行。
    for node in parsed.find_all(exp.DML, exp.DDL):
        reasons.append(
            f"write operation {node.key.upper()} is not allowed "
            f"(found inside query: {node.sql()[:80]})"
        )

    # SELECT ... INTO OUTFILE/DUMPFILE(写文件面);INTO @var 只读放行
    for node in parsed.find_all(exp.Into):
        kind = str(node.args.get("kind") or "").upper()
        if kind in INTO_WRITE_KINDS:
            reasons.append(f"SELECT INTO {kind} is not allowed (writes a file)")

    # 危险函数
    for node in parsed.find_all(exp.Func):
        name = node.name.upper()
        if name in DANGEROUS_FUNCS:
            reasons.append(f"function {node.name} is not allowed")

    # 表引用:元数据表一律拒绝;allowlist 给定则业务表必须 ⊆ 集合
    # CTE 名(如 FROM x)在 AST 里也是 exp.Table,但 x 是 CTE 不是真实表——
    # 先从树中收集全部 CTE 名(含嵌套),跳过它们。
    cte_names = {cte.alias.lower() for cte in parsed.find_all(exp.CTE)}
    allowed = {t.lower() for t in allowed_tables} if allowed_tables else None
    for table in parsed.find_all(exp.Table):
        name = table.name.lower()
        if name in cte_names:
            continue
        name = table.name.lower()
        db = (table.db or "").lower()
        catalog = (table.catalog or "").lower()
        is_meta = (
            name in META_TABLES
            or db in META_CATALOGS
            or catalog in META_CATALOGS
            or name.startswith("pg_")
        )
        if is_meta:
            reasons.append(f"table '{table.name}' is a metadata/system table")
            continue
        if allowed is not None and name not in allowed:
            reasons.append(
                f"table '{table.name}' is not in the allowed tables"
            )

    return len(reasons) == 0, reasons


def _parse(sql: str, dialect: str):
    """RAISE 解析;失败回退 mysql(反引号标识符),再失败返回 None。"""
    import sqlglot

    candidates = ([dialect] if dialect else []) + [""]
    for d in candidates:
        try:
            return sqlglot.parse_one(
                sql, dialect=d or None,
                error_level=sqlglot.ErrorLevel.RAISE,
            )
        except Exception:
            continue
    try:
        return sqlglot.parse_one(
            sql, dialect="mysql", error_level=sqlglot.ErrorLevel.RAISE,
        )
    except Exception:
        return None
