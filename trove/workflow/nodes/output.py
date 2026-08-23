"""Output node — formats the final response as Markdown.

Conclusion-first layout (data-assistant pattern): the LLM conclusion opens
the answer, then the chart (primary), the results table, and finally a
collapsible technical-detail section (SQL / semantics / assessment /
execution time / KB usage / confidence). The web UI renders the
``<details>`` wrapper as a native collapsible block; the CLI flattens it
(see cli/app.py). On graceful degradation (state.error set) it formats a
readable error section instead.

Node shape: `async def output(state: WorkflowState) -> dict`
returns a partial state update.
"""

from __future__ import annotations

from typing import Any

from trove.core.i18n import L
from trove.llm.observability import record_span
from trove.services.limits import get_result_limits
from trove.services.sql.format import format_sql
from trove.services.viz.spark import render_ascii_bar
from trove.workflow.state import WorkflowState


def _record_result(state: WorkflowState) -> None:
    """终态 span:成败原因(verdict/重试/行数/错误)进 langfuse。

    output 是全部路径的必经终点(含错误路径),成败原因在此落 trace;
    节点异常本身已由 LangGraph 回调标红。无 Langfuse 时 no-op。
    """
    with record_span(
        "workflow.result",
        input={
            "question": state.question,
            "verdict": state.verdict,
            "reason": state.reason,
            "retry_count": state.retry_count,
            "row_count": state.row_count,
            "error": state.error,
            "sql": state.sql,
        },
    ):
        pass


def _details_wrap(summary: str, body: str) -> str:
    """Collapsible detail section: web UI renders <details>, CLI flattens.

    The markdown renderer (markdown-it html:false) never sees this wrapper —
    the frontend tokenizer extracts it as its own block, and the CLI strips
    the wrapper lines to render the inner markdown flat.
    """
    return f"<details>\n<summary>{summary}</summary>\n\n{body.strip()}\n</details>"


def _build_results_table(
    lang: str,
    columns: list[str],
    rows: list[list[Any]],
    row_count: int,
    display_rows: int,
) -> str:
    """Markdown table of the result rows (respecting the display cap)."""
    parts = [L(
        lang,
        f"### 结果 ({row_count} 行)\n",
        f"### Results ({row_count} rows)\n",
    )]
    parts.append("| " + " | ".join(columns) + " |")
    parts.append("| " + " | ".join("---" for _ in columns) + " |")
    shown = 0
    for row in rows[:display_rows]:
        parts.append("| " + " | ".join(str(cell) for cell in row) + " |")
        shown += 1
    if row_count > shown:
        parts.append(L(
            lang,
            f"\n*…以及另外 {row_count - shown} 行(表格展示上限 {display_rows} 行,"
            "下载为完整查询结果)*\n",
            f"\n*... and {row_count - shown} more rows "
            f"(table shows up to {display_rows}; download includes the full result)*\n",
        ))
    return "\n".join(parts)


def _build_details(state: WorkflowState) -> str:
    """Technical detail section (SQL / semantics / assessment / meta).

    Rendered inside the collapsible <details> wrapper; empty body → "" so the
    caller omits the section entirely.
    """
    lang = state.lang
    parts: list[str] = []

    # SQL
    if state.sql:
        parts.append(f"### {L(lang, '生成的 SQL', 'Generated SQL')}\n")
        parts.append(f"```sql\n{format_sql(state.sql, state.dialect)}\n```\n")

    # Semantic explanation (生成 SQL 后的 LLM 语义说明)
    if state.semantics:
        parts.append(f"### {L(lang, '语义说明', 'Semantics')}\n")
        parts.append(f"{state.semantics}\n")

    # Reflection
    if state.verdict and state.verdict != "OK":
        line = f"**{L(lang, '评估', 'Assessment')}**: {state.verdict}"
        if state.reason:
            line += f" — {state.reason}"
        parts.append(line + "\n")

    # Metadata
    if state.execution_time_ms:
        parts.append(f"\n---\n*{L(lang, '执行耗时', 'Execution time')}: {state.execution_time_ms:.0f}ms*")

    # Multi-candidate disagreement → low-confidence note
    if not state.consensus:
        parts.append(L(
            lang,
            "\n*置信度:低(候选 SQL 结果不一致)*\n",
            "\n*Confidence: low (candidate SQLs disagreed)*\n",
        ))

    # Knowledge base usage
    if state.kb_hits:
        term_parts = [
            f"{h['term']} → {h['mapping']}"
            for h in state.kb_hits
            if h.get("kind") == "term"
        ]
        example_count = sum(1 for h in state.kb_hits if h.get("kind") == "example")
        template_count = sum(1 for h in state.kb_hits if h.get("kind") == "template")
        segments = []
        if term_parts:
            segments.append(", ".join(term_parts))
        if example_count:
            segments.append(L(lang,
                              f"{example_count} 个示例参与",
                              f"{example_count} example" + ("s" if example_count != 1 else "") + " used"))
        if template_count:
            segments.append(L(lang,
                              f"{template_count} 个确定性模板命中(快速路径)",
                              f"{template_count} template used (deterministic fast path)"))
        if segments:
            parts.append(L(
                lang,
                f"\n*知识库: {' | '.join(segments)}*\n",
                f"\n*Knowledge base: {' | '.join(segments)}*\n",
            ))

    return "\n".join(parts).strip()


async def output(state: WorkflowState) -> dict[str, Any]:
    """Format results into Markdown from the workflow state."""
    _record_result(state)
    question = state.question

    if state.clarification_question:
        response = (
            "## Clarification\n\n"
            f"**Question**: {question}\n\n"
            f"{state.clarification_question}\n"
        )
        return {"final_response": response}

    if state.intent_answer:
        return {"final_response": state.intent_answer}

    lang = state.lang
    limits = get_result_limits()
    display_rows = limits.display_rows

    if state.error:
        response = f"**{L(lang, '错误', 'Error')}**: {state.error}\n"
        return {"final_response": response}

    parts: list[str] = []

    # 1. Conclusion — LLM one-sentence direct answer (结论前置)
    if state.conclusion:
        parts.append(f"### {L(lang, '结论', 'Conclusion')}\n")
        parts.append(f"{state.conclusion}\n")

    # 2. Chart — primary visual (ASCII for CLI; web renders ECharts)
    has_ascii_chart = False
    if state.chart:
        ascii_chart = render_ascii_bar(state.chart, lang)
        if ascii_chart:
            has_ascii_chart = True
            parts.append(f"\n{ascii_chart}\n")

    # 3. Results — data table (collapsible detail when a chart is present,
    #    chart-primary layout; otherwise shown directly)
    if state.columns:
        table = _build_results_table(
            lang, state.columns, state.rows, state.row_count, display_rows,
        )
        if has_ascii_chart:
            parts.append(_details_wrap(
                L(lang, "结果明细", "Results detail"),
                table,
            ) + "\n")
        else:
            parts.append(table + "\n")
    elif state.row_count == 0:
        parts.append(L(lang, "**结果**: 查询返回 0 行。\n", "**Result**: Query returned zero rows.\n"))
    else:
        # No execution data — this is the "empty" workflow case
        parts.append(L(lang, "(未执行任何查询)\n", "(No query executed)\n"))

    # Insights (执行后 LLM 生成的洞察)
    if state.insights:
        parts.append(f"### {L(lang, '洞察', 'Insights')}\n")
        for insight in state.insights:
            parts.append(f"- {insight}")
        parts.append("\n")

    # 4. Collapsible technical details (SQL / semantics / meta)
    details = _build_details(state)
    if details:
        parts.append(_details_wrap(
            L(lang, "查看 SQL 与详情", "View SQL & details"),
            details,
        ) + "\n")

    response = "\n".join(parts)

    return {"final_response": response}
