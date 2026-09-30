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

from trove.agent.answer_source import AnswerSource
from trove.agent.answer_source import resolve as resolve_answer_source
from trove.agent.answer_source import source_line as render_source_line
from trove.core.i18n import L
from trove.llm.observability import record_span
from trove.services.errors import present_error
from trove.services.limits import get_result_limits
from trove.services.sql.format import format_sql
from trove.services.viz.spark import render_ascii_bar, render_waterfall_ascii
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


def _error_response(state: WorkflowState) -> tuple[str, dict[str, Any]]:
    """失败路径的对外呈现:标题/解释/下一步 + 折叠的内部细节。

    返回 (markdown, error_info)。error_info 走 summary 到前端——用户可见
    那部分由 present_error 保证不含流水线词汇,原始文本一字不动地留在
    detail.raw 里,折叠区与日志仍可归因。
    """
    lang = state.lang
    info = present_error(state.error, lang=lang)
    head = (
        f"**{L(lang, '错误', 'Error')}**: {info['title']}\n\n"
        f"{info['explanation']}\n\n"
        f"{info['suggestion']}\n"
    )
    warning = _still_running_notice(state)
    if warning:
        head += f"\n{warning}"
    body = (
        f"**{L(lang, '内部诊断信息', 'Internal diagnostics')}**\n\n"
        f"- {L(lang, '错误类别', 'Error class')}: `{info['detail']['error_class']}`\n"
        f"- {L(lang, '失败节点', 'Failed node')}: `{info['detail']['node']}`\n"
        f"- {L(lang, '原始信息', 'Raw message')}: `{info['detail']['raw']}`\n"
    )
    kill_line = _kill_detail_line(state)
    if kill_line:
        body += kill_line
    markdown = head + "\n" + _details_wrap(
        L(lang, "技术细节", "Technical details"), body,
    ) + "\n"
    return markdown, info


#: 终止结果 → 这一行怎么说(设计 §7.3 / §10 / I4)。**三态各说各的**:
#: 表里没有 ``""`` —— 没试过就没有这一行,而不是一行写着「未知」的话。
#: 措辞刻意不写「已终止」:``kill_sent`` 只表示指令交出去了,服务端有没有停
#: 我们不知道(``QueryTerminator`` 的返回值就这么定的),这里不许替它加码。
_KILL_DETAIL: dict[str, tuple[str, str]] = {
    "kill_sent": ("已发出终止指令(服务端未经确认)",
                  "stop signal sent (not acknowledged by the server)"),
    "kill_unsupported": ("该数据源不支持主动终止查询",
                         "this datasource cannot be asked to stop a query"),
    "kill_failed": ("终止指令未能发出", "the stop signal could not be sent"),
}

#: 后果是「那条查询还在跑」的两种终止结果 —— 它们才配一句正文告警。
#: ``kill_sent`` 不在其中:指令出去了,用户没有要做的动作,喊一声只是噪音。
_MAY_STILL_BE_RUNNING = ("kill_unsupported", "kill_failed")


def _kill_of(state: WorkflowState) -> str:
    """超时路径写下的终止结果;``""`` = 没试过(或这一轮没超时)。"""
    return str((state.execution_evidence or {}).get("kill") or "")


def _still_running_notice(state: WorkflowState) -> str:
    """**正文**告警:查询可能还在数据源上跑(超时才可能发生)。

    为什么值得单独一句:超时在用户那里只是「这次没结果」,而在数据源那边可能
    多了一条正在跑的查询 —— 前者只能重试,后者得有人去处理。这两种处境要能
    一眼分开。
    """
    kill = _kill_of(state)
    if kill not in _MAY_STILL_BE_RUNNING:
        return ""
    lang = state.lang
    reason = _KILL_DETAIL[kill][0 if lang == "zh" else 1]
    return L(
        lang,
        f"> ⚠️ **这次超时的查询可能仍在数据源上运行**:{reason}。\n",
        "> ⚠️ **The query from this run may still be running on the "
        f"datasource**: {reason}.\n",
    )


def _kill_detail_line(state: WorkflowState) -> str:
    """内部细节区那一行(操作者在折叠区看);没试过则没有这一行。"""
    kill = _kill_of(state)
    label = _KILL_DETAIL.get(kill)
    if label is None:
        return ""
    lang = state.lang
    return (
        f"- {L(lang, '超时后的终止', 'Termination after timeout')}: "
        f"`{kill}` — {label[0 if lang == 'zh' else 1]}\n"
    )


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


#: ``as_of_basis`` → 展示名。**不翻译的口径照实报原文**(见下方),宁可生僻
#: 也不能换一个名字 —— 口径名是这句话的全部含义(R5)。
_BASIS_LABELS: dict[str, tuple[str, str]] = {
    "last_modified": ("最后修改时间", "last modified"),
    "latest_partition": ("最新分区", "latest partition"),
}


def _degradation_notice(state: WorkflowState) -> str:
    """降级执行提示(设计 §6.2 / I2 / R1),置顶在结论之上。

    **看不见的降级等于没有降级**:用户拿到一张少了几行的表,却没有任何线索
    说明它为什么少。四种组合各自说各自的话 —— 尤其是 ``degraded=True`` 且
    ``limit_applied=None`` 那一格(没依据、也没加上边界),它是唯一一格
    「连截断都没做到」的,不能借用「已截断」的话术。

    放行(``verdict != "degrade"``)一律无声:提示给多了就没人看了。
    """
    ev = state.execution_evidence or {}
    if str(ev.get("verdict") or "") != "degrade":
        return ""
    lang = state.lang
    limit = ev.get("limit_applied")
    degraded = bool(ev.get("degraded"))
    est = ev.get("estimated_rows")
    soft = (ev.get("budget") or {}).get("soft_scan_rows")

    if limit is not None:
        if degraded:
            body = L(
                lang,
                f"本次查询的扫描量无法估算;已为结果加上行数上限(最多 {limit} 行)。",
                "the scan size could not be estimated; the result was capped at "
                f"{limit} rows.",
            )
        else:
            body = L(
                lang,
                f"本次查询估算扫描 {est} 行,超过本部署的 {soft} 行软限;"
                f"已为结果加上行数上限(最多 {limit} 行)。",
                f"this query is estimated to scan {est} rows, over this "
                f"deployment's {soft}-row soft cap; the result was capped at "
                f"{limit} rows.",
            )
    elif degraded:
        body = L(
            lang,
            "本次查询的扫描量无法估算,也没能加上行数上限 —— 本次查询的代价未知。",
            "the scan size could not be estimated and no row cap could be "
            "applied — the cost of this query is unknown.",
        )
    else:
        body = L(
            lang,
            f"本次查询估算扫描 {est} 行,超过本部署的软限,但没能加上行数上限 "
            "—— 结果可能过大。",
            f"this query is estimated to scan {est} rows, over this "
            "deployment's soft cap, but no row cap could be applied — the "
            "result may be very large.",
        )
    return L(
        lang,
        f"> ⚠️ **降级执行**:{body}\n",
        f"> ⚠️ **Degraded execution**: {body}\n",
    )


def _validator_notice(state: WorkflowState) -> str:
    """org validator 的 advisory 判词,与降级提示同区置顶。

    **只报 ``verdict is False`` 的 advisory**,三条都不报:

    - ``verdict is None``(判不了)—— 它最常见的原因是"这次的结果集里恰好
      没有点名的列",挂在多处时高频出现。投到用户面前就是每次查询多一句
      "本次未能校验 XX 口径",两周内用户就会开始无视所有附注,把唯一有意义
      的告警一起淹掉。它说的是**管理员**(编写问题),去处是质检统计。
    - ``blocking`` —— 违反已经被拦下重算;能走到 output 说明要么预算耗尽
      后交付、要么本轮没判 False。前者由错误卡片负责。
    - ``verdict is True`` —— 通过就是无声。
    """
    hits = [
        h for h in (state.validator_hits or [])
        if h.get("severity") == "advisory" and h.get("verdict") is False
    ]
    if not hits:
        return ""
    body = "; ".join(
        f"[{h.get('name', '')}] {h.get('message', '')}" for h in hits
    )
    return L(
        state.lang,
        f"> ⚠️ **口径提示**：{body}\n",
        f"> ⚠️ **Caliber note**: {body}\n",
    )


def _freshness_line(state: WorkflowState) -> str:
    """数据截止时间(设计 §8.4 / I5 / R5);不复述就返回 ""。

    三个状态**必须分得开**(与 ``execution_evidence`` 里的三态一一对应):

    * ``basis=""`` —— 没查过(没装画像)→ 一个字都不说。说「无从判断」是撒谎:
      那是在讲「我查了但查不到」,而我们根本没查。
    * ``basis="unknown"`` —— 查过了,无从判断 → **明写**。沉默也是一种谎:
      用户会把「没提」读成「没问题」。
    * 有值 —— 值与口径**同行**(R5):单看一个 ``2026-09-01`` 会被读成
      「数据已更新到此刻」,口径才是它的含义。
    """
    ev = state.execution_evidence or {}
    basis = str(ev.get("as_of_basis") or "")
    if not basis:
        return ""
    lang = state.lang
    if basis == "unknown":
        return L(
            lang,
            "*数据截止时间:无从判断(画像里没有可用的依据)*\n",
            "*Data as of: unknown (no usable basis in the table profile)*\n",
        )
    label = _BASIS_LABELS.get(basis)
    shown = (label[0] if lang == "zh" else label[1]) if label else basis
    return L(
        lang,
        f"*数据截止时间:{ev.get('data_as_of')}(依据:{shown})*\n",
        f"*Data as of: {ev.get('data_as_of')} (basis: {shown})*\n",
    )


def _answer_source(state: WorkflowState) -> AnswerSource | None:
    """本轮答案的来源档位;``None`` = 没有可披露的答案。**判定只在这里发生一次**。

    门是 ``state.sql``:来源说的是**这条答案**怎么来的 —— 没执行过查询(元数据
    问答、澄清反问、空跑)就没有可说的;错误路径同理,那里交付的是错误卡片,不是
    答案。这条门必须和渲染同源:两处各判一次,迟早出现「行说已认证、字段说生成」
    —— 那比不披露更坏。

    统计段(``runs`` / ``p50_ms``)今天一律缺席:两个数在台账里(设计 §6.2 的
    ``AssetLedger``),读取侧还没接线。**不猜、不编**——文案本身不依赖统计,拿不到
    就退化成只报档位那一档(见 ``answer_source.source_line``);接线后把两个数传
    进来即可,这个函数不用动。
    """
    if not state.sql:
        return None
    return resolve_answer_source(state)


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

    # Metadata + 成本证据。I7:估算给出来;实际扫描量适配器报不出时**不写**,
    # 详情区里一行恒为「未知」的数字只会训练人忽略这一整块。
    meta_lines: list[str] = []
    if state.execution_time_ms:
        meta_lines.append(L(
            lang,
            f"执行耗时: {state.execution_time_ms:.0f}ms",
            f"Execution time: {state.execution_time_ms:.0f}ms",
        ))
    estimated = (state.execution_evidence or {}).get("estimated_rows")
    if estimated is not None:
        meta_lines.append(L(
            lang, f"估算扫描行数: {estimated}", f"Estimated scan rows: {estimated}",
        ))
    if meta_lines:
        parts.append("\n---\n" + "\n".join(f"*{line}*" for line in meta_lines))

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


def _build_attribution_section(state: WorkflowState) -> str:
    """归因分析区块:叙事 + 归因表 + 瀑布图(ASCII 兜底)。

    全部来自 state.attribution(attribution 节点产物);字段缺失即跳过对应
    小节。叙事缺失只出表(分析本身照常可见),不阻断回答。比率指标
    (kind == "ratio")渲染率/权重列 + 三效应汇总行。
    """
    attr = state.attribution or {}
    lang = state.lang
    parts: list[str] = [f"### {L(lang, '归因分析', 'Attribution')}\n"]

    if attr.get("narrative"):
        parts.append(f"{attr['narrative']}\n")

    # 比率指标的三效应汇总(本征/结构/交叉 = 总变化)
    is_ratio = str(attr.get("kind") or "") == "ratio"
    effects = attr.get("effects")
    if is_ratio and isinstance(effects, dict):
        eff_lines = []
        if lang == "zh":
            eff_lines = [
                f"- {L(lang, '本征效应', 'Within effect')}: {effects.get('within', 0.0):g}",
                f"- {L(lang, '结构效应', 'Composition effect')}: {effects.get('composition', 0.0):g}",
                f"- {L(lang, '交叉效应', 'Interaction effect')}: {effects.get('interaction', 0.0):g}",
            ]
        else:
            eff_lines = [
                f"- Within effect: {effects.get('within', 0.0):g}",
                f"- Composition effect: {effects.get('composition', 0.0):g}",
                f"- Interaction effect: {effects.get('interaction', 0.0):g}",
            ]
        parts.append("\n".join(eff_lines) + "\n")

    # 瀑布图(ECharts 主 / ASCII 兜底)——与主结果图表独立区块
    chart = attr.get("chart")
    if chart:
        ascii_chart = render_waterfall_ascii(chart, lang)
        if ascii_chart:
            parts.append(f"\n{ascii_chart}\n")

    table = attr.get("table") or []
    if table:
        baseline_label = {
            "prev_period": "基期" if lang == "zh" else "Base",
            "yoy": "去年同期" if lang == "zh" else "YoY",
            "share": "本期" if lang == "zh" else "Current",
        }.get(str(attr.get("baseline") or ""), "基期")
        if is_ratio:
            parts.append(f"| {L(lang, '维度', 'Dimension')} | "
                         f"{L(lang, '基期率', 'Base rate')} | "
                         f"{L(lang, '当前率', 'Current rate')} | "
                         f"{L(lang, '基期权重', 'Base weight')} | "
                         f"{L(lang, '当前权重', 'Current weight')} | "
                         f"{L(lang, '变化量', 'Δ')} | "
                         f"{L(lang, '贡献率', 'Contribution')} |")
            parts.append("| --- | --- | --- | --- | --- | --- | --- |")
            for it in table:
                parts.append(
                    f"| {it.get('dim', '')} | {it.get('base_rate', 0.0):g} | "
                    f"{it.get('current_rate', 0.0):g} | "
                    f"{it.get('base_weight', 0.0):.1%} | "
                    f"{it.get('current_weight', 0.0):.1%} | "
                    f"{it.get('delta', 0.0):g} | "
                    f"{it.get('contribution', 0.0):g} |"
                )
        else:
            parts.append(f"| {L(lang, '维度', 'Dimension')} | {baseline_label} | "
                         f"{L(lang, '当前', 'Current')} | {L(lang, '变化量', 'Δ')} | "
                         f"{L(lang, '贡献率', 'Contribution')} |")
            parts.append("| --- | --- | --- | --- | --- |")
            for it in table:
                parts.append(
                    f"| {it.get('dim', '')} | {it.get('base', 0.0):g} | "
                    f"{it.get('current', 0.0):g} | {it.get('delta', 0.0):g} | "
                    f"{it.get('contribution', 0.0):+.1%} |"
                )
        parts.append("\n")

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
        response, error_info = _error_response(state)
        # 错误路径**不**写 answer_source:档位说的是「这条**答案**是怎么来的」,
        # 而这一轮交付的是错误卡片(它自带错误类别与下一步,见 _error_response),
        # 没有答案就没有来源。不写 = 保持本轮输入全量重置后的空串。
        return {"final_response": response, "error_info": error_info}

    parts: list[str] = []

    # 0. 降级提示 — 必须在结论**之上**:结论是要读的第一句,读者得先知道它
    #    的数据被削过(I2 / R1)。
    notice = _degradation_notice(state)
    if notice:
        parts.append(notice)

    # 0b. 口径提示 — 与降级提示同区、同一理由:都是"读完结论之前就该知道的
    #     前提"(数据被削过 / 某项口径没满足)。窄口:只报 advisory 的明确
    #     违反,详见 _validator_notice。
    #     位置在这里是**必须**的:它在 `if state.error:` 的提前 return 之后。
    #     规则失败返回可能让上一轮的 validator_hits 残留下来,而错误/降级路径
    #     不该把那份陈旧判定渲染给用户看。
    vnotice = _validator_notice(state)
    if vnotice:
        parts.append(vnotice)

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

    # 3b. 数据截止时间 —— 紧挨着结果。**在折叠区之外**:读结果的人不展开详情
    #     也该看到它(R5 的展示前提)。
    freshness = _freshness_line(state)
    if freshness:
        parts.append(freshness)

    # 3c. 答案来源(设计 §7.3)—— 与截止时间同一位置、同一理由:一条讲数据来
    #     自哪一刻,一条讲答案来自哪条路径,都是读者不展开详情也该看到的东西。
    #     I6:档位只由 _answer_source 判定一次,渲染与 state 字段同源。
    source = _answer_source(state)
    if source is not None:
        parts.append(render_source_line(source, lang=state.lang))

    # Insights (执行后 LLM 生成的洞察)
    if state.insights:
        parts.append(f"### {L(lang, '洞察', 'Insights')}\n")
        for insight in state.insights:
            parts.append(f"- {insight}")
        parts.append("\n")

    # 归因分析(业务级根因:为什么/贡献类问题的多跳下钻结果)
    if state.attribution:
        attr_parts = _build_attribution_section(state)
        if attr_parts:
            parts.append(attr_parts + "\n")

    # 4. Collapsible technical details (SQL / semantics / meta)
    details = _build_details(state)
    if details:
        parts.append(_details_wrap(
            L(lang, "查看 SQL 与详情", "View SQL & details"),
            details,
        ) + "\n")

    response = "\n".join(parts)

    # 档位单独落进 state:markdown 里那一行是给人读的,这个字段是给机器读的
    # (SSE 的 summary / 前端视觉区分 / 台账落账)。两者同源同上文,不许各判一次。
    return {
        "final_response": response,
        "answer_source": source.value if source is not None else "",
    }
