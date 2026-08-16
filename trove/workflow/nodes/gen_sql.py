"""GenSQL nodes — generate SQL from natural language with a validate-retry loop.

The loop is composed as a subgraph (see workflow/graphs.py) from two node
functions built here:
  - generate: builds the prompt (initial or fix), calls the LLM, extracts SQL
  - validate: validates via SQLGlot; routes back to generate on failure

Pure helpers (prompt builders, SQL extraction, validation) live at module
level for direct unit testing.
"""

from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from trove.core.config import AgentConfig
from trove.core.i18n import L
from trove.core.logging import get_logger
from trove.llm.gateway import LLMGateway
from trove.workflow.state import GenSQLState

logger = get_logger(__name__)

# ── Prompt Templates ─────────────────────────────────────

SQL_GENERATION_SYSTEM_PROMPT = """You are a SQL generation assistant. Your task is to translate natural language questions into accurate SQL queries.

Guidelines:
1. Generate ONLY the SQL query, nothing else.
2. Use the provided table schemas exactly as described.
3. Use standard SQL syntax that works with the target dialect.
4. If the question is ambiguous, make reasonable assumptions and note them.
5. Do NOT use INSERT, UPDATE, DELETE, DROP, or any write operations.
6. Always use proper quoting for table and column names when needed.
7. If you cannot generate a valid SQL query, explain why.
8. When the Evidence defines a formula (e.g. "Gap = X - Y" or "rate = (A - B) / B * 100"), apply it at the scope the formula states. Unless the Evidence explicitly restricts the scope, the formula's terms ("highest/lowest/average X") are computed GLOBALLY over the whole table — do not anchor a global term to one specific row or entity mentioned elsewhere in the question.
9. For percentage/ratio calculations over counts, use CAST(SUM(cond) AS DOUBLE) * 100 / COUNT(...) — cast to floating point BEFORE dividing, and return the full-precision result. Integer division in MySQL truncates to 4 decimal places (27/61*100 yields 44.2623 instead of 44.26229508196721), which breaks exact result matching.
10. Unit consistency: the question's own wording decides WHAT is being measured. "Percentage of accounts/clients/rows" → count rows (SUM(cond) / COUNT(*)); "percentage of amount/total amount" → sum amounts (SUM(CASE cond THEN amount) / SUM(amount)). When the Evidence formula's unit conflicts with the question's unit, the question's unit wins — Evidence resolves column/value meanings, not what is counted.
11. Reference examples are authoritative: when a Reference example's question closely matches the current question, treat its SQL as the standard formulation — reproduce its joins, filters, grouping, and result columns exactly. Do not "improve" or reinterpret it.
12. Result granularity: a plain "list the X ..." question (no ranking words like "top N") returns ONE ROW PER MATCHING RECORD — do NOT add SELECT DISTINCT or DISTINCT inside aggregate functions (e.g. AVG(DISTINCT x), SUM(DISTINCT x)) to collapse duplicates unless the question explicitly asks for unique/unduplicated values (e.g. "unique", "distinct", "different X"). COUNT(DISTINCT) is allowed when the question asks for the NUMBER of distinct entities ("number of districts", "how many different X"). Only ranking questions ("top ten X by Y") may deduplicate to one row per Y.
13. Answer columns: output ONLY the columns the question asks for. For a "list all the X" question that does not name columns, output just the identifying column of X (its ID — e.g. trans_id, account_id) or the attribute the question names; do NOT dump the full record (dates, amounts, symbols, statuses) unless the question explicitly names them. Do NOT append extra identification columns: when the question mentions people ("who are the account holders") but names specific columns to state ("State the account ID and the frequency"), output exactly those named columns — no extra person/entity ID (e.g. client_id), and no extra joins whose only purpose is to "identify" them.
14. Age computation: "age" questions compute age as the simple YEAR difference, e.g. DATE_FORMAT(CAST(CURRENT_TIMESTAMP() AS DATETIME), '%Y') - DATE_FORMAT(CAST(birth_date AS DATETIME), '%Y'). Do NOT use TIMESTAMPDIFF(YEAR, birth_date, ...) — it subtracts one more year when the birthday has not passed yet this year, producing off-by-one ages that break exact result matching.
15. Minimum/maximum: "lowest/highest X" questions use ORDER BY col ASC/DESC LIMIT 1 — plain and sufficient. Do NOT wrap them in CTEs or nested MIN/MAX subqueries; those add syntax risk with no benefit.
16. Combined superlatives: "oldest AND lowest X" (youngest AND highest, etc.) is ONE ordering: ORDER BY <primary> ASC, <secondary> ASC LIMIT 1 — the primary condition decides, the secondary only breaks ties. Do NOT pre-filter by the global MIN/MAX of the secondary condition first.
17. Join paths: when the table in question carries its own FK column (e.g. client.district_id), join directly on that column. Do NOT route through association tables (disp) to reach the same table — that changes the row granularity and the result.
18. Entity vs metric: for "the X with the biggest/smallest Y" questions, select or group by the entity column the question names (e.g. the region name), not by the metric column (e.g. inhabitants count).

Output format:
```sql
SELECT ...
```
"""

SQL_GENERATION_SYSTEM_PROMPT_ZH = """你是 SQL 生成助手，负责把自然语言问题翻译成准确的 SQL 查询。

规则：
1. 只输出 SQL 查询，不要输出其他内容。
2. 严格使用提供的表结构。
3. 使用符合目标方言的标准 SQL 语法。
4. 问题有歧义时做合理假设并在 SQL 注释中说明。
5. 禁止 INSERT、UPDATE、DELETE、DROP 等写操作。
6. 需要时正确引用表名和列名。
7. 无法生成合法 SQL 时，说明原因。
8. 当 Evidence 给出公式定义（如 "Gap = X - Y" 或 "rate = (A - B) / B * 100"）时，按其字面作用域执行：除非 Evidence 明确限定范围，公式中的"最高/最低/平均 X"都在整表全局计算，不要把全局项锚定到问题中提到的某个具体行或实体上。
9. 百分比/比例计算遵循 BIRD 惯例：CAST(SUM(条件) AS DOUBLE) * 100 / COUNT(...) —— 先转浮点再除，返回全精度结果。MySQL 整数除法会截断到 4 位小数（27/61*100 = 44.2623，而不是 44.26229508196721），导致结果无法精确匹配。
10. 口径一致性：问题自身措辞决定"数什么"。"percentage of accounts/clients/rows" → 按行数统计（SUM(条件) / COUNT(*)）；"percentage of amount/total amount" → 按金额求和（SUM(CASE 条件 THEN amount) / SUM(amount)）。当 Evidence 公式的口径与问题措辞冲突时，以问题措辞为准——Evidence 只解决列/取值含义，不决定统计对象。
11. 参考示例是权威的：当某个 Reference example 的问题与当前问题高度相似时，把它的 SQL 视为标准写法——精确复刻其 join、过滤、分组和结果列，不要"改进"或重新解读它。
12. 结果粒度：普通 "list the X ..." 问题（不含 "top N" 等排序词）应每个匹配记录返回一行——不要加 SELECT DISTINCT，也不要在聚合函数内加 DISTINCT（如 AVG(DISTINCT x)、SUM(DISTINCT x)）去重，除非问题明确要求唯一/去重值（如 "unique"、"distinct"、"different X"）。问题询问"不同实体的数量"（"number of districts"、"how many different X"）时允许 COUNT(DISTINCT)。只有排序类问题（"top ten X by Y"）才按 Y 去重为一行。
13. 答案列：只输出问题要求的列。"list all the X" 类问题若未指明列，只输出 X 的标识列（其 ID，如 trans_id、account_id）或问题点名的属性列；不要输出整条记录的日期、金额、符号、状态等额外列，除非问题明确点名这些列。不要附加额外的识别列：问题提到人物（"who are the account holders"）但点名了要输出的列（"State the account ID and the frequency"）时，只输出这些点名的列——不要为了"识别"人物而追加 client_id 等实体 ID，也不要为此增加多余的 join。
14. 年龄计算："age" 问题按简单年份差计算年龄，如 DATE_FORMAT(CAST(CURRENT_TIMESTAMP() AS DATETIME), '%Y') - DATE_FORMAT(CAST(birth_date AS DATETIME), '%Y')。不要用 TIMESTAMPDIFF(YEAR, birth_date, ...)——当年生日未过时它会少算一岁，与标准答案差 1 岁，导致结果无法精确匹配。
15. 极值问题："最低/最高的 X" 直接用 ORDER BY 列 ASC/DESC LIMIT 1 即可，不要包 CTE 或嵌套 MIN/MAX 子查询——徒增语法风险。
16. 多重最高级："最年长且 X 最低"（最年轻且最高等）是单条排序：ORDER BY 主条件, 次条件 LIMIT 1——主条件定胜负，次条件只裁决平局。不要先按次条件的全局 MIN/MAX 预过滤。
17. 连接路径：相关表自带外键列时（如 client.district_id）直接在该列上 join；不要为了到达同一张表绕道关联表（disp）——那会改变行粒度与结果。
18. 实体与度量："拥有最多/最少 Y 的 X" 类问题按问题点名的实体列（如地区名）选择或分组，而不是按度量列（如居民数量）。

输出格式：
```sql
SELECT ...
```
"""

SQL_FIX_PROMPT = """The following SQL query failed validation:

```sql
{sql}
```

Validation errors:
{errors}

Please fix the SQL query. Keep the original query intent — fix syntax errors only, do not change the business semantics. Generate ONLY the corrected SQL.

```sql
SELECT ...
```
"""

SQL_FIX_PROMPT_ZH = """以下 SQL 查询未通过校验：

```sql
{sql}
```

校验错误：
{errors}

请修复该 SQL。保持原始查询意图不变，只修正语法错误，不要改变问题的业务语义。只输出修正后的 SQL，不要输出其他内容。

```sql
SELECT ...
```
"""


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
    rules: list[str] | None = None,
    lessons: list[dict[str, Any]] | None = None,
    few_shots: list[dict[str, Any]] | None = None,
    term_notes: list[dict[str, Any]] | None = None,
) -> str:
    """Build the initial SQL generation prompt.

    Knowledge base material (optional) is injected as:
      - Terminology: standard formulations for matched business terms
      - Reference examples: top-K similar questions with their SQL
    """
    parts = [
        f"Target SQL dialect: {dialect}",
        "",
        "Database schema:",
        schema_context or "(No schema information available - generate a best-effort query)",
        "",
    ]
    if history:
        parts += [
            "Conversation history (previous exchanges, oldest first):",
            history,
            "",
        ]
    if plan:
        parts += [
            "Query plan (follow it unless it conflicts with the question):",
            plan,
            "",
        ]
    if rules:
        parts.append("Data source rules (must follow):")
        for rule in rules:
            parts.append(f"- {rule}")
        parts.append("")
    if lessons:
        parts.append("Known pitfalls (learned from past corrections — avoid these):")
        for lesson in lessons:
            parts.append(f"- {lesson.get('pattern', '')}: {lesson.get('note', '')}")
        parts.append("")
    if term_notes:
        parts.append("Terminology (standard formulations):")
        for note in term_notes:
            line = f"- {note.get('term', '')} → {note.get('mapping', '')}"
            if note.get("definition"):
                line += f" — {note['definition']}"
            parts.append(line)
        parts.append("")
    if few_shots:
        parts.append("Reference examples (standard formulations for this data):")
        for shot in few_shots:
            parts.append(f"Q: {shot.get('question', '')}")
            parts.append(f"SQL: {shot.get('sql', '')}")
        parts.append("")
    # Evidence and the resolved time range sit right before the question —
    # the last things the model reads before generating, and authoritative
    # over its own assumptions (a wrong assumption here is silent, not
    # visible).
    if evidence:
        parts += [
            "Evidence (official hint for this question — authoritative, must follow):",
            evidence,
            "",
        ]
    if time_context:
        parts += [
            "Resolved time range (authoritative — derived from the question's relative time expression; use it for date filters):",
            time_context,
            "",
        ]
    parts += [
        "Question:",
        question,
        "",
    ]
    if reflect_reason:
        parts += [
            f"Note: a previous version of this query was rejected with: "
            f"{reflect_reason}. Please correct it.",
            "",
        ]
    if error_feedback:
        parts += [
            f"Note: the previous query failed during execution with: "
            f"{error_feedback}. Please correct the SQL.",
            "",
        ]
    if error_analysis:
        parts += [
            "Error analysis (diagnosis and fix plan from the expert):",
            error_analysis,
            "",
        ]
    if reasoning_context:
        parts += [
            "Prior reasoning trail (your previous thinking; avoid repeating the same mistake):",
            reasoning_context,
            "",
        ]
    parts.append("Generate the SQL query to answer this question:")
    return "\n".join(parts)


def build_fix_prompt(sql: str, errors: list[str], lang: str = "en") -> str:
    """Build a fix prompt when validation fails (bilingual; default en keeps
    the pure-helper behavior for direct callers)."""
    template = L(lang, SQL_FIX_PROMPT_ZH, SQL_FIX_PROMPT)
    return template.format(
        sql=sql,
        errors="\n".join(f"- {e}" for e in errors),
    )


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
            prompt = build_sql_prompt(
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
                rules=state.rules or None,
                lessons=state.lessons or None,
                few_shots=state.few_shots or None,
                term_notes=state.term_notes or None,
            )

        model = config.target or "openai/gpt-4o"
        start = time.monotonic()
        system_prompt = L(
            state.lang,
            SQL_GENERATION_SYSTEM_PROMPT_ZH,
            SQL_GENERATION_SYSTEM_PROMPT,
        )
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
