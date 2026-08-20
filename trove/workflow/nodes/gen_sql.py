"""GenSQL nodes — generate SQL from natural language with a validate-retry loop.

The loop is composed as a subgraph (see workflow/graphs.py) from two node
functions built here:
  - generate: builds the prompt (initial or fix), calls the LLM, extracts SQL
  - validate: validates via SQLGlot; routes back to generate on failure

Pure helpers (prompt builders, SQL extraction, validation) live at module
level for direct unit testing.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from trove.core.config import AgentConfig
from trove.core.logging import get_logger
from trove.llm.gateway import LLMGateway
from trove.prompts import render
from trove.workflow.rules import verify
from trove.workflow.state import GenSQLState
from trove.workflow.versions import EXEC_FAILURE_SIG

logger = get_logger(__name__)


def build_sql_prompt(
    question: str,
    schema_context: str,
    dialect: str,
    reflect_reason: str = "",
    error_feedback: str = "",
    history: str = "",
    plan: str = "",
    evidence: str = "",
    time_context: str = "",
    error_analysis: str = "",
    reasoning_context: str = "",
    rejected_hypotheses: list[dict[str, str]] | None = None,
    previous_sql: str = "",
    sql_versions: list[dict[str, Any]] | None = None,
    fix_mode: str = "",
    rules: list[str] | None = None,
    lessons: list[dict[str, Any]] | None = None,
    few_shots: list[dict[str, Any]] | None = None,
    term_notes: list[dict[str, Any]] | None = None,
    lang: str = "en",
) -> str:
    """Build the initial SQL generation prompt.

    Knowledge base material (optional) is injected as:
      - Terminology: standard formulations for matched business terms
      - Reference examples: top-K similar questions with their SQL

    Thin wrapper over the ``gen_sql/user`` Jinja template.
    """
    return render(
        "gen_sql/user",
        lang=lang,
        question=question,
        schema_context=schema_context,
        dialect=dialect,
        reflect_reason=reflect_reason,
        error_feedback=error_feedback,
        history=history,
        plan=plan,
        evidence=evidence,
        time_context=time_context,
        error_analysis=error_analysis,
        reasoning_context=reasoning_context,
        rejected_hypotheses=rejected_hypotheses or [],
        previous_sql=previous_sql,
        sql_versions=render_versions(sql_versions or []),
        fix_mode=fix_mode,
        rules=rules or [],
        lessons=lessons or [],
        few_shots=few_shots or [],
        term_notes=term_notes or [],
    )


def build_sql_prompt_from_state(state: GenSQLState) -> str:
    """从子图状态装配生成 prompt:集中 17 个字段的展开(与 build_sql_prompt 一致)。

    消除 graphs.py 与 make_generate 两处各自列 17 个 kwargs 的重复——新增
    注入字段只改这一处 + build_sql_prompt 签名。
    """
    return build_sql_prompt(
        question=state.question,
        schema_context=state.schema_context,
        dialect=state.dialect,
        reflect_reason=state.reflect_reason,
        error_feedback=state.error_feedback,
        history=state.history,
        plan=state.plan,
        evidence=state.evidence,
        time_context=state.time_context,
        error_analysis=state.error_analysis,
        reasoning_context=state.reasoning_context,
        rejected_hypotheses=state.rejected_hypotheses or None,
        previous_sql=state.previous_sql,
        sql_versions=state.sql_versions or None,
        fix_mode=state.fix_mode,
        rules=state.rules or None,
        lessons=state.lessons or None,
        few_shots=state.few_shots or None,
        term_notes=state.term_notes or None,
        lang=state.lang,
    )


def build_fix_prompt(sql: str, errors: list[str], lang: str = "en") -> str:
    """Build a fix prompt when validation fails (bilingual; default en keeps
    the pure-helper behavior for direct callers)."""
    return render("gen_sql/fix", lang=lang, sql=sql, errors=errors)


def render_shots(shots: list[dict[str, Any]]) -> str:
    """参考示例段文本（token 估算用）——与 gen_sql/user 模板格式一致。"""
    return "".join(
        f"Q: {s.get('question', '')}\nSQL: {s.get('sql', '')}\n" for s in shots
    )


def render_terms(terms: list[dict[str, Any]]) -> str:
    """术语段文本（token 估算用）——与 gen_sql/user 模板格式一致。"""
    return "".join(
        f"- {t.get('term', '')} → {t.get('mapping', '')}"
        + (f" — {t['definition']}" if t.get("definition") else "")
        + "\n"
        for t in terms
    )


def render_lessons(lessons: list[dict[str, Any]]) -> str:
    """教训段文本（token 估算用）——与 gen_sql/user 模板格式一致。"""
    return "".join(
        f"- {l.get('pattern', '')}: {l.get('note', '')}\n" for l in lessons
    )


def render_rules(rules: list[str]) -> str:
    """业务规则段文本（token 估算用）——与 gen_sql/user 模板格式一致。"""
    return "".join(f"- {r}\n" for r in rules)


def render_cache_prefix(dialect: str, schema_context: str) -> str:
    """稳定可缓存前缀（dialect + schema）——gen_sql/user 模板最前面的两节。

    prompt caching 的稳定前缀：同一数据源 + 方言下字节级一致，跨调用
    可复用（Anthropic/OpenAI 对 stable prefix 打折）。与模板的渲染输出
    逐字一致，避免估算失真；被归入 context_usage 的 cache_prefix_tokens。
    """
    schema = schema_context or (
        "(No schema information available - generate a best-effort query)"
    )
    return f"Target SQL dialect: {dialect}\n\nDatabase schema:\n{schema}\n"


def render_versions(versions: list[dict[str, Any]]) -> str:
    """失败版本链文本（定点修复用）：每版 SQL + 结果签名 + 规则命中。"""
    if not versions:
        return ""
    parts = []
    for v in versions:
        issues = ", ".join(v.get("issues") or []) or "-"
        sql_short = " ".join((v.get("sql") or "").split())
        sig = v.get("sig") or ""
        if sig == EXEC_FAILURE_SIG:
            # 执行错误:签名无意义,展示原始错误文本
            err = (v.get("error") or "")[:120]
            result_part = f"result: exec-error ({err})" if err else "result: exec-error"
        else:
            result_part = f"result: {sig[:60]}"
        parts.append(
            f"- Round {v.get('round', '?')}: {sql_short}\n"
            f"  {result_part}; issues: {issues}"
        )
    return "\n".join(parts)


def extract_sql(response: str) -> str:
    """Extract SQL from LLM response (handles markdown code blocks)."""
    # Try ```sql ... ``` block first
    match = re.search(r'```sql\s*\n(.*?)\n```', response, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()

    # Try generic ``` ... ``` block
    match = re.search(r'```\s*\n(.*?)\n```', response, re.DOTALL)
    if match:
        return match.group(1).strip()

    # If no code block, return the raw response (strip common prefixes)
    text = response.strip()
    for prefix in ["SELECT", "WITH", "select", "with"]:
        idx = text.find(prefix)
        if idx >= 0:
            return text[idx:]

    return text


def validate_sql(sql: str, dialect: str) -> tuple[bool, list[str]]:
    """Validate SQL using SQLGlot.

    Returns:
        (is_valid, list_of_error_strings)
    """
    try:
        import sqlglot
        from sqlglot import exp

        errors = []

        try:
            parsed = sqlglot.parse(
                sql, dialect=dialect,
                error_level=sqlglot.ErrorLevel.RAISE,
            )
            statements = [p for p in parsed if p is not None]
            if not statements:
                errors.append("SQL could not be parsed (empty result)")
            elif len(statements) > 1:
                errors.append("Multiple SQL statements are not allowed")
            elif not isinstance(statements[0], exp.Query):
                # e.g. "SELEC * FORM t" parses into an Alias, not a query
                errors.append("Not a valid SQL query")
        except Exception as e:
            errors.append(f"Parse error: {e}")

        return len(errors) == 0, errors

    except ImportError:
        # sqlglot not installed — skip validation
        logger.warning("sqlglot not available; skipping SQL validation")
        return True, []


# ── Probe tool (read-only execution observation) ─────────

PROBE_LIMIT = 10        # fetch 封顶
PROBE_SAMPLE_ROWS = 5   # 观测里显示的行数
PROBE_TIMEOUT_S = 5.0


def _has_limit(sql: str, dialect: str) -> bool:
    """检测顶层 LIMIT 是否已存在(sqlglot 解析;失败退化文本扫描)。

    保守方向:无法确认时按"已有 LIMIT"处理——不改写、不做 COUNT 包装
    (观测里 row_count 可能超过封顶,但不会把模型合法的 SQL 改坏)。
    """
    try:
        import sqlglot
        parsed = sqlglot.parse_one(
            sql, dialect=dialect, error_level=sqlglot.ErrorLevel.RAISE,
        )
        return parsed.args.get("limit") is not None
    except Exception:
        if re.search(r"\bLIMIT\s+\d+\b", sql, re.I):
            return True
        return True  # 无法确认 → 保守按有 LIMIT 处理


def _append_limit(sql: str, dialect: str, limit: int) -> str:
    """追加顶层 LIMIT(sqlglot 重写;失败退化文本追加)。"""
    try:
        import sqlglot
        parsed = sqlglot.parse_one(
            sql, dialect=dialect, error_level=sqlglot.ErrorLevel.RAISE,
        )
        return parsed.limit(limit, copy=False).sql(dialect=dialect)
    except Exception:
        return f"{sql.rstrip().rstrip(';')} LIMIT {limit}"


def _short_value(v: Any) -> str:
    """观测里的单值:截断为短字符串(超长文本/二进制不刷屏)。"""
    if v is None:
        return "null"
    s = str(v)
    return s[:40] + "…" if len(s) > 40 else s


async def _probe_result(
    connectors, sql: str, dialect: str,
    limit: int = PROBE_LIMIT,
    timeout_s: float = PROBE_TIMEOUT_S,
) -> dict[str, Any]:
    """共享只读执行通道:真实执行草稿 SQL,返回原始观测 dict(值不截断)。

    供 probe_query(观测)与 check_result(规则校验)复用。**永不抛异常**
    ——任何失败都折叠成 ``{"ok": False, "error": ...}``。

    双保险只读门:``SQLValidator.is_safe``(关键词正则)+ ``validate_sql``
    (sqlglot 单语句 ``exp.Query`` 校验——Insert/Update/Delete/Drop
    均非 exp.Query,天然拦截;多语句同样拒绝)。

    Returns:
        ``{"ok", "row_count", "columns"[:20], "rows"[:limit], "error"}``
        ——rows 为原始值,由调用方决定展示形式(观测截断 vs 规则校验用真值)。
    """
    if connectors is None:
        return {"ok": False, "error": "no datasource available"}
    sql = (sql or "").strip()
    if not sql:
        return {"ok": False, "error": "empty SQL"}
    from trove.services.sql.validator import SQLValidator

    if not SQLValidator().is_safe(sql):
        return {"ok": False, "error": "write operations are not permitted"}
    valid, errors = validate_sql(sql, dialect)
    if not valid:
        return {"ok": False, "error": "; ".join(errors)}

    had_limit = _has_limit(sql, dialect)
    probe_sql = sql if had_limit else _append_limit(sql, dialect, limit)
    try:
        result = await asyncio.wait_for(
            connectors.execute(probe_sql), timeout=timeout_s,
        )
    except TimeoutError:
        return {"ok": False, "error": f"probe timed out after {timeout_s}s"}
    except Exception as e:
        return {"ok": False, "error": f"execution failed: {e}"}

    rows = result.rows or []
    row_count = len(rows)
    if not had_limit:
        # 无原始 LIMIT:补 COUNT 包装拿真实行数(失败静默保留封顶值)
        try:
            count_sql = f"SELECT COUNT(*) AS _p FROM ({sql}) AS _p"
            count_result = await asyncio.wait_for(
                connectors.execute(count_sql), timeout=timeout_s,
            )
            if count_result.rows and count_result.rows[0]:
                row_count = count_result.rows[0][0]
        except Exception:
            pass
    return {
        "ok": True,
        "row_count": row_count,
        "columns": (result.columns or [])[:20],
        "rows": rows[:limit],
    }


async def probe_query(
    connectors, sql: str, dialect: str,
    timeout_s: float = PROBE_TIMEOUT_S,
) -> str:
    """只读执行探针:真实执行草稿 SQL,返回短 JSON 观测串。

    模型在定稿前用它快速验证:行数规模、列形状、过滤值是否命中
    (如最高级问题是否 0 行、自造过滤值是否有数据)。**永不抛异常**
    ——任何失败都折叠成 ``{"ok": false, "error": ...}`` 观测。

    观测形状: ``{"ok", "row_count", "columns"[:20], "rows"[:5], "error"}``
    """
    obs = await _probe_result(
        connectors, sql, dialect, limit=PROBE_LIMIT, timeout_s=timeout_s,
    )
    if not obs["ok"]:
        return json.dumps(obs)
    return json.dumps({
        "ok": True,
        "row_count": obs["row_count"],
        "columns": obs["columns"],
        "rows": [[_short_value(v) for v in r] for r in obs["rows"][:PROBE_SAMPLE_ROWS]],
    })


# ── check_result tool (deterministic rule verification) ──

CHECK_RESULT_LIMIT = 50  # 规则校验取数封顶(规则只消费行数/首行值/列形状,50 足够)


async def check_result(
    connectors,
    question: str,
    sql: str,
    dialect: str,
    lang: str = "en",
    timeout_s: float = PROBE_TIMEOUT_S,
) -> tuple[str, list[dict]]:
    """确定性校验工具:执行草稿 SQL 后跑规则链,返回 (观测文本, hits)。

    与 probe_query 的分工:probe 只给观测、判断仍靠模型;check_result
    把判断变成硬规则(workflow.rules 的注册表——结果形状、行数、值域、
    整数除法等)。违规原因即修复指令,模型拿到后直接改 SQL,而不是等
    execute→validate 事后打回(那要浪费一整轮 retry 预算)。

    返回 (text, hits):
      - 通过: ``"OK (N rows)"``,hits 为空
      - 违规: ``"VIOLATION [规则名] 原因"``,hits=[{"name", "reason"}]
      - 不可执行: ``"ERROR: ..."``,hits 为空
    """
    obs = await _probe_result(
        connectors, sql, dialect, limit=CHECK_RESULT_LIMIT, timeout_s=timeout_s,
    )
    if not obs["ok"]:
        return f"ERROR: {obs['error']}", []
    reason, hits = verify(
        question, sql, obs["columns"], obs["rows"], obs["row_count"], lang=lang,
    )
    if reason:
        return f"VIOLATION {reason}", hits
    return f"OK ({obs['row_count']} rows)", []


# ── search_values tool (value/cell retrieval) ───────────

SEARCH_VALUES_LIMIT = 10   # 每列返回的匹配值上限
SEARCH_VALUES_MAX_COLS = 10  # 未指定列时扫描的最大列数


def _like_pattern(keyword: str) -> str:
    """LIKE 通配符转义:%,_ 按字面匹配(否则 'P%' 会被当通配)。

    用 ``!`` 作 ESCAPE 字符而非反斜杠:反斜杠在 MySQL(解释转义)与
    SQLite(不解释转义)的字面量处理不一致,``!`` 在两方言都是单字符。
    """
    escaped = keyword.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    return f"%{escaped}%"


async def search_values(
    connectors,
    table: str,
    keyword: str,
    column: str | None = None,
    timeout_s: float = PROBE_TIMEOUT_S,
) -> str:
    """按关键词检索真实值:定位脏值/格式变体/拼写差异。

    - column 给定:在该列做大小写不敏感 LIKE,返回匹配的 DISTINCT 值;
    - column 省略:扫描该表前 SEARCH_VALUES_MAX_COLS 个列,返回
      ``column → 匹配值`` 映射(帮模型定位"这个值藏在哪列")。

    标识符取自已校验的 schema(无注入面);LIKE 通配符按字面转义。
    **永不抛异常**——失败折叠成 ``{"ok": false, "error": ...}``。
    """
    if connectors is None:
        return json.dumps({"ok": False, "error": "no datasource available"})
    table = (table or "").strip()
    keyword = (keyword or "").strip()
    if not table or not keyword:
        return json.dumps({"ok": False, "error": "table and keyword are required"})

    schema = await connectors.get_schema()
    target = next(
        (t for t in schema.tables if t.name.lower() == table.lower()), None,
    )
    if target is None:
        return json.dumps({"ok": False, "error": f"table '{table}' not found"})

    if column:
        cols = [c.name for c in target.columns if c.name.lower() == column.lower()]
        if not cols:
            return json.dumps({"ok": False, "error": f"column '{column}' not found in '{table}'"})
        obs = await _search_one(connectors, table, cols[0], keyword, timeout_s)
        if not obs["ok"]:
            return json.dumps(obs)
        # 工具契约:始终返回 JSON 串(_search_one 返回 dict,单列路径不得泄漏)
        return json.dumps({
            "ok": True, "table": table, "column": cols[0], "values": obs["values"],
        })

    # 未指定列:扫描前 N 列,返回 column → 匹配值 映射。
    # 单列失败不能静默吞掉(否则方言/类型错误会被谎报成"无匹配"),
    # 首错上抛;真无匹配才返回空映射。
    hits: dict[str, list] = {}
    first_error: str | None = None
    for c in target.columns[:SEARCH_VALUES_MAX_COLS]:
        obs = await _search_one(connectors, table, c.name, keyword, timeout_s)
        if obs.get("ok"):
            if obs.get("values"):
                hits[c.name] = obs["values"]
        elif first_error is None:
            first_error = obs.get("error")
    if first_error and not hits:
        return json.dumps({"ok": False, "error": first_error})
    if not hits:
        return json.dumps({
            "ok": True, "table": table, "hits": {},
            "note": f"no column contains a value matching '{keyword}'",
        })
    return json.dumps({"ok": True, "table": table, "hits": hits})


async def _search_one(
    connectors, table: str, column: str, keyword: str, timeout_s: float,
) -> dict:
    """单列 LIKE 检索:返回 {"ok", "values"} 或 {"ok": False, "error"}。"""
    pattern = _like_pattern(keyword)
    sql = (
        f"SELECT DISTINCT `{column}` FROM `{table}` "
        f"WHERE CAST(`{column}` AS CHAR) LIKE '{pattern}' ESCAPE '!' "
        f"LIMIT {SEARCH_VALUES_LIMIT}"
    )
    try:
        result = await asyncio.wait_for(connectors.execute(sql), timeout=timeout_s)
    except TimeoutError:
        return {"ok": False, "error": f"search timed out after {timeout_s}s"}
    except Exception as e:
        return {"ok": False, "error": f"execution failed: {e}"}
    values = [_short_value(r[0]) for r in (result.rows or [])]
    return {"ok": True, "values": values}


# ── Tool factory (gen_sql ReAct 循环的工具集合) ─────────


def build_sql_registry(
    connectors,
    question: str,
    lang: str,
    dialect: str,
    *,
    finish: bool = True,
):
    """gen_sql ReAct 循环的注册表工厂:返回注册表(已注册工具 + 归因切片)。

    validate_sql 始终可用(纯语法校验,不执行);probe_query / check_result
    依赖 connectors(只读执行),connectors 缺失时自动降级为仅语法工具。
    check_result 的规则命中累积在 ``registry.check_hits``,循环结束后由
    调用方带出到状态(validation_hits 归因切片)——不再是位置返回值。
    注册表自带显式 finish 协议:模型用 ``finish(answer)`` 携带最终 SQL
    定稿,避免答案丢失。

    Args:
        connectors: 数据源注册表(None → 降级,无执行类工具)。
        question: 当前问题(规则校验的判定输入,由节点注入)。
        lang: 交互语言(规则原因文本本地化)。
        dialect: SQL 方言(sqlglot 校验/重写用)。
        finish: 是否注册显式 finish 工具(harness 协议;legacy 层关闭)。
    """
    from trove.llm.agent_loop import ToolRegistry

    registry = ToolRegistry(finish=finish)
    registry.check_hits = []

    async def validate_tool(arguments: dict) -> str:
        valid, errors = validate_sql(arguments.get("sql", ""), dialect)
        if valid:
            return "valid"
        return "ERRORS: " + "; ".join(errors)

    registry.register(
        "validate_sql", validate_tool,
        description="Validate a SQL query for syntax. Returns 'valid' or a list of errors.",
        parameters={
            "type": "object",
            "properties": {"sql": {"type": "string", "description": "The SQL to validate"}},
            "required": ["sql"],
        },
    )

    if connectors is None:
        return registry

    async def probe_tool(arguments: dict) -> str:
        # 只读执行探针:模型定稿前快速验证草稿 SQL 的形状与行数
        return await probe_query(connectors, arguments.get("sql", ""), dialect)

    async def check_tool(arguments: dict) -> str:
        # 确定性规则校验:probe 之后、定稿之前,把"判断"变成硬规则
        text, hits = await check_result(
            connectors, question, arguments.get("sql", ""), dialect, lang=lang,
        )
        registry.check_hits.extend(hits)
        return text

    async def search_tool(arguments: dict) -> str:
        # 值检索:定位脏值/格式变体/拼写差异,锚定过滤值到真实数据
        return await search_values(
            connectors,
            arguments.get("table", ""),
            arguments.get("keyword", ""),
            arguments.get("column"),
        )

    async def lookup_schema_tool(arguments: dict) -> str:
        # 懒加载表 DDL:预算裁掉未注入的表时,模型按需取列/主键/外键
        table = (arguments.get("table") or "").strip()
        if not table:
            return '{"ok": false, "error": "table is required"}'
        try:
            schema = await connectors.get_schema()
        except Exception as e:
            return '{"ok": false, "error": "schema unavailable: %s"}' % (e,)
        target = next(
            (t for t in schema.tables if t.name.lower() == table.lower()), None,
        )
        if target is None:
            return '{"ok": false, "error": "table not found: %s"}' % (table,)
        cols = ", ".join(c.name for c in target.columns)
        pk = ", ".join(
            c.name for c in target.columns if c.primary_key
        ) or "-"
        fks = "; ".join(
            f"{c.name} → {c.foreign_key}"
            for c in target.columns if c.foreign_key
        ) or "-"
        return json.dumps({
            "ok": True,
            "table": target.name,
            "columns": cols,
            "primary_key": pk,
            "foreign_keys": fks,
        })

    registry.register(
        "probe_query", probe_tool,
        description=(
            "Execute a SQL query read-only and return a short observation: "
            '{"ok", "row_count", "columns", "rows" (first 5)}. '
            "Fetches at most 10 rows, 5s timeout, never modifies data. "
            "Use BEFORE finalizing a draft to verify result shape, row count, "
            "and that filter values actually match data (e.g. a superlative "
            "question returning 0 rows, or a self-invented filter value)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "The SQL to probe (read-only)"},
            },
            "required": ["sql"],
        },
    )
    registry.register(
        "check_result", check_tool,
        description=(
            "Run the deterministic rule checks against your draft SQL "
            "(executes it read-only): result shape, row count, value "
            "ranges, integer-division ratios. Returns 'OK (N rows)' or "
            "'VIOLATION [rule] <reason>' — the reason is the fix "
            "instruction. Call AFTER probe_query and BEFORE finalizing: "
            "a VIOLATION means fix the SQL, never finalize a violating draft."
        ),
        parameters={
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "The SQL to check (read-only)"},
            },
            "required": ["sql"],
        },
    )
    registry.register(
        "search_values", search_tool,
        description=(
            "Search a table for distinct values matching a keyword "
            "(case-insensitive LIKE on real data). Use when you have a "
            "candidate filter value from the question or Evidence but must "
            "confirm its exact real form (spelling, case, abbreviations, "
            "dirty values), or when you don't know which column stores a "
            "value — omit the column to scan the table and get a "
            "column→values map. Returns at most 10 values per column."
        ),
        parameters={
            "type": "object",
            "properties": {
                "table": {"type": "string", "description": "Table to search in"},
                "keyword": {"type": "string", "description": "Value fragment to match (case-insensitive)"},
                "column": {"type": "string", "description": "Optional: restrict the search to one column"},
            },
            "required": ["table", "keyword"],
        },
    )
    registry.register(
        "lookup_schema", lookup_schema_tool,
        description=(
            "Fetch the full schema of a single table: columns, primary "
            "key, foreign keys. Use when a table is referenced in the "
            "question but was NOT listed in the Database schema section "
            "(budget pruning omits low-signal tables from the prompt) — "
            "fetch it here instead of guessing its columns."
        ),
        parameters={
            "type": "object",
            "properties": {
                "table": {"type": "string", "description": "Table name to describe"},
            },
            "required": ["table"],
        },
    )
    return registry


def make_sql_tools(
    connectors,
    question: str,
    lang: str,
    dialect: str,
) -> tuple[list[dict], dict[str, Callable[[dict[str, Any]], Awaitable[str]]], list[dict]]:
    """gen_sql ReAct 循环的工具工厂(legacy 形态):返回 (tools, handlers, hits_sink)。

    registry 形态见 build_sql_registry;本函数为向后兼容保留——返回
    纯定义列表 + handler 字典(不含 finish 工具)。
    """
    from trove.llm.agent_loop import ToolRegistry

    registry = build_sql_registry(
        connectors, question, lang, dialect, finish=False,
    )
    tools: list[dict] = registry.defs()
    handlers: dict[str, Callable[[dict[str, Any]], Awaitable[str]]] = registry.handlers()
    return tools, handlers, registry.check_hits


# ── Subgraph nodes ───────────────────────────────────────


def make_generate(
    llm: LLMGateway,
    config: AgentConfig,
    temperature: float = 0.0,
) -> Callable[[GenSQLState], Awaitable[dict[str, Any]]]:
    """Build the generate node: prompt → LLM → extract SQL.

    Args:
        temperature: Sampling temperature (0.0 deterministic; the
            alternative multi-candidate subgraph uses a higher value).
    """

    async def generate(state: GenSQLState) -> dict[str, Any]:
        if state.error:
            return {}

        if state.validation_errors and state.sql:
            prompt = build_fix_prompt(state.sql, state.validation_errors, lang=state.lang)
        else:
            # 上一轮产出为空(SQL 为空):没有可"修复"的对象,
            # 回到原始生成提示词重试,而不是让模型去修一条空 SQL。
            prompt = build_sql_prompt_from_state(state)

        model = config.model_for(state.complexity)
        start = time.monotonic()
        system_prompt = render("gen_sql/system", lang=state.lang)
        response = await llm.chat(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            temperature=temperature,
            metadata={
                "node": "gen_sql",
                "session_id": state.session_id,
                "run_id": state.run_id,
                "question": state.question[:80],
            },
        )
        sql = extract_sql(response)

        return {
            "sql": sql,
            "attempts": state.attempts + 1,
            "validation_errors": [],
            "llm": {
                "model": model,
                "elapsed_ms": int((time.monotonic() - start) * 1000),
                "input_preview": prompt[:200],
                "output_preview": response[:200],
            },
        }

    return generate


def make_validate(
    max_retries: int = 3,
) -> Callable[[GenSQLState], Awaitable[dict[str, Any]]]:
    """Build the validate node: SQLGlot check, error bookkeeping, exhaustion."""

    async def validate(state: GenSQLState) -> dict[str, Any]:
        if state.error:
            return {}

        if not state.sql:
            errors = ["LLM returned empty SQL"]
        else:
            valid, errors = validate_sql(state.sql, state.dialect)
            if valid:
                return {"validation_errors": []}

        if state.attempts >= max_retries:
            return {
                "error": (
                    f"SQL generation failed after {state.attempts} attempts: "
                    + "; ".join(errors)
                ),
                "validation_errors": errors,
            }
        return {"validation_errors": errors}

    return validate
