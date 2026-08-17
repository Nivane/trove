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
from trove.core.logging import get_logger
from trove.llm.gateway import LLMGateway
from trove.prompts import render
from trove.workflow.state import GenSQLState

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
        rules=rules or [],
        lessons=lessons or [],
        few_shots=few_shots or [],
        term_notes=term_notes or [],
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
