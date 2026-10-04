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
from trove.agent.confidence import (
    confidence_enabled, render_line, result_confidence, sql_confidence,
)
from trove.core.i18n import L
from trove.llm.observability import record_span
from trove.services.errors import present_error
from trove.services.limits import get_result_limits
from trove.services.skills.validators import format_hit
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
    # 判词是手写 YAML 的自由文本(validator 档唯一的授权路径就是手写):
    # 空名字留下 ``[] 判词`` 的残缺排版,换行会把引用块冲出三行、把后面的
    # 内容顶成正文。两处同一渲染的另一处是 ``validate.py`` 的 blocking join。
    body = "; ".join(format_hit(h) for h in hits)
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


def _build_details(
    state: WorkflowState, source: AnswerSource | None, *,
    show_confidence: bool, sql_score: float,
) -> str:
    """Technical detail section (SQL / semantics / assessment / meta).

    Rendered inside the collapsible <details> wrapper; empty body → "" so the
    caller omits the section entirely.

    ``source`` 由调用方传入而**不在这里再判一次**:档位的判定只发生一次
    (``_answer_source``),渲染与 state 字段必须同源。传参而不是读闭包变量,
    是让「只有一份判定」在签名上就看得见。

    ``show_confidence`` 是急停开关(§6.2),由调用方**算好传进来** —— 与决定
    「枚举行放哪儿」用的是同一个值,不在这里再调一次 ``confidence_enabled()``。
    关掉时这一段整体退回今天的形态:折叠区内没有 SQL 置信度行,来源枚举行也
    不在这里(它在披露面)。§6.2 那张表的右格与验收第 11 条就是这条契约。

    ``sql_score`` 同理**是参数,不是 ``state.sql_confidence``**:那个字段的唯一
    写点是 ``output`` 自己末尾的返回值,而本函数在它**之前**跑 —— 从字段读
    永远读到默认值 ``0.0``,这一行会一次都不出现(实测,不是推演)。披露面与
    证据面因此拿的是**同一个** ``sql_score``:一个数字,两处渲染。
    """
    lang = state.lang
    parts: list[str] = []

    # SQL
    if state.sql:
        parts.append(f"### {L(lang, '生成的 SQL', 'Generated SQL')}\n")
        parts.append(f"```sql\n{format_sql(state.sql, state.dialect)}\n```\n")
        # SQL 置信度:分数跟着**它的对象**走 —— SQL 本来就在折叠区里,所以
        # 它不必上披露面。这里回答的是披露面那个数字的「哪个环节弱」。
        # **开关关掉就没有这一行** —— §6.2 表的右格写死「SQL(无 SQL 置信度)」,
        # 验收第 11 条同:「折叠区内无 SQL 置信度」。不加这个门,急停就只停了
        # 披露面、折叠区里还留着一个新形态的分数 —— 正是 §6.2 说要避免的
        # 「半个功能」。
        if show_confidence and sql_score > 0:
            parts.append(L(
                lang,
                f"*SQL 置信度: {round(sql_score * 100)}%*\n",
                f"*SQL confidence: {round(sql_score * 100)}%*\n",
            ))

    # 答案来源(原在折叠区**外**,决策三移入这里)—— 它是分数的**证据**:
    # 用户看到 82% 追问「凭什么」,第一层答案就是档位。判定仍只发生一次
    # (``_answer_source``),这里只是换个位置渲染。
    # **同样受开关管**:关掉时调用方已经在披露面渲染了它,这里再渲染一份就是
    # 同一个来源说两遍 —— 那不是「今天的形态」,是新造出来的第三种(§6.2)。
    if show_confidence and source is not None:
        parts.append(render_source_line(source, lang=lang))

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

    # (已移除)Multi-candidate disagreement → low-confidence note。
    # 它判的那件事(``not state.consensus``)已经被结果置信度的票率折损吸收
    # —— 同一件事不两处说(设计 §5.4-3)。

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


def _tree_rows(node: dict[str, Any], depth: int = 0) -> list[tuple[int, dict[str, Any]]]:
    """驱动器树 → [(深度, 节点)] 先序展开(渲染缩进表用)。"""
    rows: list[tuple[int, dict[str, Any]]] = [(depth, node)]
    for child in node.get("children") or []:
        if isinstance(child, dict):
            rows.extend(_tree_rows(child, depth + 1))
    return rows


def _tree_note(node: dict[str, Any], lang: str) -> str:
    """树节点说明列:执行状态 + 残差诚实性(恒等式成立/残差/组件缺失)。"""
    if not node.get("executed"):
        return str(node.get("note") or L(lang, "未取到值", "no value"))
    res = node.get("residual")
    reason = str((res or {}).get("reason") or "")
    if reason == "identity":
        return L(lang, "恒等式成立", "identity holds")
    if reason == "gap":
        val = (res or {}).get("value")
        return (f"{L(lang, '残差', 'residual')} {val:g}"
                if isinstance(val, (int, float)) else L(lang, "残差", "residual"))
    if reason == "component_unavailable":
        return L(lang, "组件未取到、不声称分解", "component unavailable")
    if reason == "non_decomposable":
        return L(lang, "不可分解(只报值)", "non-decomposable (value only)")
    if node.get("informational"):
        return L(lang, "参考值", "informational")
    return str(node.get("note") or "")


#: 块粒度 → 展示词(zh, en);未知粒度原样拼「块」,不猜。
_GRAIN_WORDS = {
    "day": ("日", "day"), "week": ("周", "week"), "month": ("月", "month"),
}

#: 带降级原因 → 展示词(zh, en);未知原因原样带出(不吞,也不编一个说法)。
_BAND_REASON_WORDS = {
    "insufficient_n": ("样本不足", "insufficient sample"),
    "no_data": ("无历史数据", "no historical data"),
    "zero_scale": ("历史值无波动(稳健尺度为零)", "no variation in history"),
}


def _numf(v: Any) -> float | None:
    """数值读取:bool 不算数;NaN/inf 读不出 → None(不编 0)。"""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        f = float(v)
        if f == f and f not in (float("inf"), float("-inf")):
            return f
    return None


def _fmt_g(v: float | None) -> str:
    return f"{v:g}" if v is not None else "—"


def _noise_band_line(series: dict[str, Any], lang: str) -> str:
    """块序列 → 「噪声带/置信」一行(B8;分析侧的显著性表述)。

    三态如实:超出带 / 落在带内 / 判不了(带不可用、本期值缺失各写明
    原因)。「样本不足」标与「位置分数(非概率)」限定语不可省 —— 块数
    不够时结论照给、标照挂。数字读不出不编 0。
    """
    b = series.get("band") if isinstance(series.get("band"), dict) else {}
    grain = str(series.get("grain") or "")
    gw = _GRAIN_WORDS.get(grain)
    unit = L(lang, f"{gw[0]}块" if gw else "块",
             f"{gw[1]}-block" if gw else "block")
    n = _numf(b.get("n"))
    if n is None and isinstance(series.get("values"), list):
        n = float(len(series["values"]))
    lookback = _numf(series.get("lookback"))
    count_txt = (f"{int(n)}" if n is not None
                 else f"{int(lookback)}" if lookback is not None else "?")
    center, scale = _numf(b.get("center")), _numf(b.get("scale"))
    lo, hi = _numf(b.get("lo")), _numf(b.get("hi"))
    current = _numf(series.get("current"))
    z, conf = _numf(series.get("z")), _numf(series.get("confidence"))
    degraded = ([str(d) for d in b["degraded"]]
                if isinstance(b.get("degraded"), list) else [])
    idx = 0 if lang == "zh" else 1
    reason_words = [_BAND_REASON_WORDS.get(d, (d, d))[idx] for d in degraded]

    if lo is None or hi is None:
        reasons = (("、".join(reason_words) if lang == "zh"
                    else ", ".join(reason_words))
                   or L(lang, "原因未记录", "reason unrecorded"))
        head = L(lang,
                 f"近 {count_txt} 个{unit}的噪声带不可用（{reasons}）",
                 f"noise band unavailable over the last {count_txt} {unit}s ({reasons})")
    else:
        head = L(lang,
                 f"近 {count_txt} 个{unit}（中位数 {_fmt_g(center)}，"
                 f"稳健尺度 {_fmt_g(scale)}），带 [{_fmt_g(lo)}, {_fmt_g(hi)}]",
                 f"over the last {count_txt} {unit}s (median {_fmt_g(center)}, "
                 f"robust scale {_fmt_g(scale)}), band [{_fmt_g(lo)}, {_fmt_g(hi)}]")

    cur_bits: list[str] = []
    if current is None:
        cur_bits.append(L(lang, "本期值缺失", "current value missing"))
    else:
        cur_bits.append(L(lang, f"本期值 {_fmt_g(current)}", f"current {_fmt_g(current)}"))
        if z is not None:
            cur_bits.append(L(lang, f"稳健 z={z:.2f}", f"robust z={z:.2f}"))
        if conf is not None:
            cur_bits.append(L(
                lang, f"位置分数 {conf:.2f}（非概率）",
                f"position score {conf:.2f} (not a probability)"))

    sep = ("；", "，") if lang == "zh" else ("; ", ", ")
    colon = "：" if lang == "zh" else ": "
    line = (f"{L(lang, '**噪声带**', '**Noise band**')}{colon}"
            f"{head}{sep[0]}{sep[1].join(cur_bits)}")
    if current is not None:
        outside = series.get("outside")
        if outside is True:
            verdict = L(lang, "超出噪声带", "outside the noise band")
        elif outside is False:
            verdict = L(lang, "落在噪声带内", "within the noise band")
        else:
            verdict = L(lang, "判不了", "undecidable")
        line += f" → {verdict}"
    if lo is not None and (series.get("low_n") is True or "insufficient_n" in degraded):
        line += L(lang, "（样本不足，带估计仅供参考）",
                  " (low sample; band is indicative only)")
    return line


def _build_attribution_section(state: WorkflowState) -> str:
    """归因分析区块:叙事 + 归因表 + 瀑布图(ASCII 兜底)+ 驱动器树。

    叙事/表/效应/瀑布来自 state.attribution(attribution 节点产物);驱动器树
    来自 state.analysis(分析柱结构化产物,缺失即跳过 —— 老路径逐字不变)。
    字段缺失即跳过对应小节。叙事缺失只出表(分析本身照常可见),不阻断
    回答。比率指标(kind == "ratio")渲染率/权重列 + 三效应汇总行。
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

    baseline_label = {
        "prev_period": "基期" if lang == "zh" else "Base",
        "yoy": "去年同期" if lang == "zh" else "YoY",
        "share": "本期" if lang == "zh" else "Current",
    }.get(str(attr.get("baseline") or ""), "基期")

    table = attr.get("table") or []
    if table:
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

    # 噪声带/置信(块序列在场才渲染;B8):本期值 vs 历史块分布的稳健带 ——
    # 分析侧的「显著性」表述。序列缺席(未启用/降级)整段不渲染,老路径逐字不变。
    series = (state.analysis or {}).get("series")
    if isinstance(series, dict):
        band_line = _noise_band_line(series, lang)
        if band_line:
            parts.append(f"{band_line}\n")

    # 驱动器树:指标按表达式分解(分析柱结构化产物,state.analysis 独有;
    # 单叶树 = 没得拆 → 不渲染,避免与上面整体对比重复)。值缺失 → "—",
    # 残差不精确如实写进说明列(宁可不拆,不造恒等式)。
    tree = (state.analysis or {}).get("tree")
    if isinstance(tree, dict) and tree.get("children"):
        parts.append(f"**{L(lang, '驱动因素分解', 'Driver decomposition')}**\n")
        parts.append(f"| {L(lang, '组件', 'Component')} | {baseline_label} | "
                     f"{L(lang, '当前', 'Current')} | {L(lang, '变化量', 'Δ')} | "
                     f"{L(lang, '说明', 'Note')} |")
        parts.append("| --- | --- | --- | --- | --- |")

        def _fmt_tree_val(v: Any) -> str:
            return f"{v:g}" if isinstance(v, (int, float)) and not isinstance(v, bool) else "—"

        for depth, node in _tree_rows(tree):
            name = "　" * depth + str(node.get("name") or node.get("metric") or "")
            parts.append(
                f"| {name} | {_fmt_tree_val(node.get('base'))} | "
                f"{_fmt_tree_val(node.get('current'))} | "
                f"{_fmt_tree_val(node.get('delta'))} | {_tree_note(node, lang)} |"
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

    # 3c. 答案级置信度(决策三,2026-09-30)。原位置是答案来源那一行 ——
    #     披露面换成了分数,枚举行移入 _build_details(§5.5)。
    #
    #     **判定只发生一次**:``source`` 在这里定,下游 _build_details 通过
    #     参数接收(而不是自己再调一次 _answer_source)。同源同上文是 I6 的
    #     实现方式,不是巧合 —— 两处各判一次,迟早出现「行说已认证、字段说
    #     生成」。
    #
    #     开关关掉 = **完整回退**到枚举披露形态(§6.2):急停要能真的停下来,
    #     所以回退的是整个决策三,不是半个。两个分数**照算照写** —— 只在渲染
    #     处分流,否则急停会顺带打瞎 avg_confidence 这个刚复活的指标。
    # 先判档位,再用它算分 —— 同一个 source 后面还要喂给 _build_details
    source = _answer_source(state)
    sql_score, sql_evidence = sql_confidence(state, source, lang=lang)
    total, discounts = result_confidence(state, sql_score, lang=lang)
    # 开关**只读一次**:它同时决定「披露面放分数还是放枚举」与「折叠区里渲染
    # 什么」(后者经 show_confidence 传给 _build_details)。读两次就可能出现
    # 「披露面放了枚举、折叠区也放一份」——那不是回退,是新造的第三种(§6.2)。
    on = confidence_enabled()
    if not on:
        if source is not None:
            parts.append(render_source_line(source, lang=lang))
    elif total > 0:
        parts.append(render_line(total, sql_evidence + discounts, lang=lang))

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
    details = _build_details(
        state, source, show_confidence=on, sql_score=sql_score,
    )
    if details:
        parts.append(_details_wrap(
            L(lang, "查看 SQL 与详情", "View SQL & details"),
            details,
        ) + "\n")

    response = "\n".join(parts)

    # 档位单独落进 state:markdown 里那一行是给人读的,这个字段是给机器读的
    # (SSE 的 summary / 前端视觉区分 / 台账落账)。两者同源同上文,不许各判一次。
    # 三个置信度字段同理:SSE / 评测 / 历史回放读的是这里,不是 markdown。
    return {
        "final_response": response,
        "answer_source": source.value if source is not None else "",
        # 无答案时保持全量重置后的 0.0(与 answer_source 的空串同一套三态)。
        "sql_confidence": sql_score,
        "confidence": total,
        "confidence_evidence": sql_evidence + discounts,
    }
