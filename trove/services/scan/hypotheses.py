"""假设层 —— LLM 只**起草**,裁决权在确定性一侧。

分工是刻意的:提出「为什么」需要语言与领域联想(LLM 擅长),判定
「是不是」只需要两个数(确定性代码擅长,而且可复算)。假设永远不
直接进结论:

  - ``propose`` —— LLM 按发现起草候选假设;**解析期先验证引用**
    (度量/维度必须在语义层里解析得出来)—— 引用不成立的候选零查询
    即被拒(还带着原因),不浪费一次执行;
  - ``verify`` —— 逐条用两个确定性查询(当期 + 基线)裁决
    ``supported | refuted | unverifiable | no_data``。每条假设**恰好
    一行 outcome**,绝不静默丢弃:判不了也要把"判不了"写下来。

四个状态的边界(与扫描器同一套诚实纪律):

  - ``unverifiable`` = 结构上无法裁决(引用解析不了、编译 MISS、预算
    耗尽、执行失败、基线窗口解析不了);
  - ``no_data`` = 查询跑了、但某一侧**没有行**(缺失 ≠ 0:没有数据
    不等于数据是零,更不等于假设被推翻);
  - ``refuted`` = 两侧都有数,方向或幅度不成立 —— 这是真的判过。
"""

from __future__ import annotations

import json
import re
from typing import Any

from trove.core.logging import get_logger
from trove.services.analysis.budget import QueryLedger
from trove.services.analysis.engine import (
    compile_hop,
    resolve_dim_ref,
    resolve_metric,
    resolve_time_field,
    time_conds,
)
from trove.services.scan.models import (
    Finding,
    Hypothesis,
    ScanError,
    VerifiedHypothesis,
)

logger = get_logger(__name__)

#: 证据节保留的结果行(与 scanner 一致)。
EVIDENCE_KEEP_ROWS = 5


# ── 解析 ─────────────────────────────────────────────

def _extract_json(raw: str) -> Any:
    """从 LLM 文本里取 JSON(容忍围栏与前后缀闲话)。取不到 → None。"""
    text = str(raw or "").strip()
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    for open_ch, close_ch in (("[", "]"), ("{", "}")):
        i, j = text.find(open_ch), text.rfind(close_ch)
        if 0 <= i < j:
            try:
                return json.loads(text[i:j + 1])
            except ValueError:
                continue
    return None


def parse_candidates(raw: Any) -> tuple[list[Hypothesis], list[dict[str, str]]]:
    """LLM 输出 → (结构合法的 Hypothesis, 结构不合格的记录)。

    单条坏候选**不拖垮整批**(丢掉一条好候选比丢掉一条坏候选昂贵);
    坏候选带着原因进 ``rejected`` —— 拒绝本身也是要看得见的结论。
    """
    data = raw if isinstance(raw, (list, dict)) else _extract_json(raw)
    if data is None:
        return [], [{"claim": "", "reason": "unparseable_response"}]
    items = data.get("hypotheses") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return [], [{"claim": "", "reason": "no_hypothesis_list"}]
    accepted: list[Hypothesis] = []
    rejected: list[dict[str, str]] = []
    for item in items:
        claim = str(item.get("claim", ""))[:200] if isinstance(item, dict) else ""
        try:
            accepted.append(Hypothesis.from_dict(item))
        except (ScanError, TypeError, ValueError) as e:
            rejected.append({"claim": claim, "reason": f"structure: {str(e)[:200]}"})
    return accepted, rejected


def reference_issue(semantic_layer: Any, h: Hypothesis) -> str:
    """引用在语义层里解析不过 → 原因字符串;通过 → ""。**零查询**。"""
    metric = resolve_metric(semantic_layer, h.metric)
    if metric is None:
        return f"unknown_metric: {h.metric}"
    matched = [str(d) for d in (getattr(metric, "datasets", None) or []) if str(d)]
    if not matched:
        return "no_anchor_dataset"
    if h.dimension and not resolve_dim_ref(semantic_layer, matched, h.dimension):
        return f"unknown_dimension: {h.dimension}"
    if not resolve_time_field(semantic_layer, matched, h.metric):
        return "no_time_field"
    return ""


def validate(
    semantic_layer: Any, candidates: list[Hypothesis],
) -> tuple[list[Hypothesis], list[dict[str, str]]]:
    """解析期引用验证:不过的候选到此为止(永不进 verify → 零查询)。"""
    accepted: list[Hypothesis] = []
    rejected: list[dict[str, str]] = []
    for h in candidates:
        reason = reference_issue(semantic_layer, h)
        if reason:
            rejected.append({"claim": h.claim, "reason": reason})
        else:
            accepted.append(h)
    return accepted, rejected


# ── 起草 ─────────────────────────────────────────────

def findings_block(findings: list[Finding], *, limit: int = 5) -> str:
    """超带发现 → 提示词里的紧凑文本(每条一行,够推断方向与量级)。"""
    lines: list[str] = []
    for f in findings[:limit]:
        lo, hi = f.band.get("lo"), f.band.get("hi")
        z = "—" if f.z is None else f"{f.z:.2f}"
        cur = "—" if f.current is None else f"{f.current:g}"
        where = f" [{f.dimension}={f.value}]" if f.dimension else ""
        lines.append(
            f"- {f.metric}{where}: 当期 {cur}, 噪声带 [{lo}, {hi}], "
            f"z={z}, n={f.n}")
    return "\n".join(lines)


async def propose(
    llm: Any,
    *,
    semantic_layer: Any,
    data: str,
    lang: str = "zh",
    limit: int = 3,
    model: str = "",
    intro: str = "",
) -> tuple[list[Hypothesis], list[dict[str, str]]]:
    """LLM 起草候选假设 → 引用验证 → (通过, 被拒记录)。

    步骤严格有序:先起草,再解析,再验证 —— 验证不过的候选**一次查询
    都不花**。``limit`` 在验证之后截断:垃圾候选不占好候选的位。

    ``model`` 非空时覆盖选模(节点层传 ``model_for_node``,让假设轮与
    节点本体用同一个模型档);``intro`` 非空时替换模板的开场白 ——
    数据来自扫描就讲噪声带,来自归因表就讲变动分解,**给模型的
    前言不许撒谎**。
    """
    if llm is None:
        raise ScanError("no LLM gateway for hypothesis drafting")
    from trove.prompts import render

    prompt = render(
        "scan/hypothesis", lang=lang, data=data, limit=int(limit),
        intro=str(intro or ""))
    model = (model or "").strip() or getattr(llm, "model", None) \
        or getattr(llm, "target", None)
    response = await llm.chat(
        model=model or "openai/gpt-4o",
        messages=[
            {"role": "system", "content": (
                "You draft falsifiable data hypotheses. "
                "Output ONLY a JSON object per the contract — no prose, no fences."
            )},
            {"role": "user", "content": prompt},
        ],
    )
    candidates, rejected = parse_candidates(response)
    accepted, more = validate(semantic_layer, candidates)
    rejected = rejected + more
    if len(accepted) > max(int(limit), 0):
        for h in accepted[max(int(limit), 0):]:
            rejected.append({"claim": h.claim, "reason": "over_limit"})
        accepted = accepted[: max(int(limit), 0)]
    return accepted, rejected


# ── 裁决 ─────────────────────────────────────────────

def _value(rows: list[list[Any]] | None) -> float | None:
    """查询结果 → 单个数值;没有行 / 值不可解析 → **None**(绝不返回 0)。"""
    if not rows:
        return None
    row = rows[-1]
    if not row:
        return None
    cell = row[-1]
    if cell is None or isinstance(cell, bool):
        return None
    if isinstance(cell, (int, float)):
        return float(cell)
    s = str(cell).strip().replace(",", "").lstrip("¥$€£￥").rstrip("%")
    try:
        return float(s)
    except ValueError:
        return None


def _row(
    h: Hypothesis, status: str, *, observed: dict[str, Any] | None = None,
    reason: str = "", queries: int = 0,
) -> VerifiedHypothesis:
    return VerifiedHypothesis(
        hypothesis=h, status=status, observed=observed or {},
        reason=reason, queries=int(queries))


async def _fetch(
    *, semantic_layer: Any, runner: Any, datasource: str, dialect: str,
    metric_name: str, matched: list[str], dim_ref: str,
    extra_conds: list[dict[str, Any]],
    time_field: str, window: tuple[str, str], purpose: str, hypothesis: str,
    ledger: QueryLedger, evidence: list[dict[str, Any]],
) -> tuple[float | None, str, str | None]:
    """一跳只读取值 → (值|None, 状态, 原因)。

    值 None 时状态非空:``unverifiable``(结构问题)或 ``no_data``
    (跑通了、行是空的)。返回原因文本只在失败时有意义。

    ``matched`` 必须给(度量的锚定数据集):``compile_hop`` 只按 matched
    在模型里解析字段,给空表会得到 compile_miss —— 那条假设会带着一个
    看起来"合理"的 unverifiable 静默永远判不了。
    """
    sql = compile_hop(
        semantic_layer, list(matched), dialect, metric_name,
        [dim_ref] if dim_ref else [],
        time_conds(time_field, window, dialect=dialect) + extra_conds,
    )
    if sql is None:
        return None, "unverifiable", "compile_miss"
    try:
        cols, rows = await runner(sql, datasource)
    except Exception as e:  # noqa: BLE001 — 执行失败是一条裁决,不是崩
        return None, "unverifiable", f"query_failed: {str(e)[:160]}"
    ledger.record("hypothesis")
    evidence.append({
        "purpose": purpose,
        "hypothesis": hypothesis[:200],
        "sql": sql,
        "columns": list(cols),
        "row_count": len(rows),
        "rows": [list(r) for r in rows[:EVIDENCE_KEEP_ROWS]],
        "truncated": len(rows) > EVIDENCE_KEEP_ROWS,
    })
    value = _value(rows)
    if value is None:
        return None, "no_data", f"no rows in {window[0]}..{window[1]}"
    return value, "", None


def _adjudicate(h: Hypothesis, cur: float, base: float) -> tuple[str, dict[str, Any], str]:
    """(当期, 基线) → (状态, observed, 原因)。纯函数,零 I/O。"""
    up = h.direction == "up"
    observed: dict[str, Any] = {"current": cur, "baseline": base}
    if base == 0.0:
        if h.min_pct > 0:
            # 相对变化的分母是 0 → 幅度不可比;方向还能看,但声明了最小
            # 幅度就说明「幅度」是假设的一部分 —— 判不了,不硬判。
            return "unverifiable", observed, "baseline_zero (relative change undefined)"
        ok = cur > base if up else cur < base
        observed["pct_change"] = None
    else:
        pct = (cur - base) / abs(base)
        observed["pct_change"] = pct
        ok = pct >= h.min_pct if up else pct <= -h.min_pct
    if ok:
        return "supported", observed, ""
    shown = (
        "n/a" if observed.get("pct_change") is None
        else f"{observed['pct_change']:+.1%}")
    want = f"{h.direction} ≥ {h.min_pct:.0%}"
    return "refuted", observed, f"observed {shown} vs claimed {want}"


async def verify(
    hypotheses: list[Hypothesis],
    *,
    semantic_layer: Any,
    runner: Any,
    datasource: str,
    dialect: str,
    window: tuple[str, str] | None,
    base_window: tuple[str, str] | None,
    ledger: QueryLedger,
    evidence: list[dict[str, Any]],
) -> list[VerifiedHypothesis]:
    """逐条裁决 —— 每条假设**恰好一行** outcome(输入序保持不变)。"""
    out: list[VerifiedHypothesis] = []
    for h in hypotheses or []:
        out.append(await _verify_one(
            h, semantic_layer=semantic_layer, runner=runner,
            datasource=datasource, dialect=dialect,
            window=window, base_window=base_window,
            ledger=ledger, evidence=evidence))
    return out


async def _verify_one(
    h: Hypothesis,
    *,
    semantic_layer: Any,
    runner: Any,
    datasource: str,
    dialect: str,
    window: tuple[str, str] | None,
    base_window: tuple[str, str] | None,
    ledger: QueryLedger,
    evidence: list[dict[str, Any]],
) -> VerifiedHypothesis:
    """单条:结构性检查(零查询)→ 预算闸 → 两跳取值 → 纯函数裁决。"""
    metric = resolve_metric(semantic_layer, h.metric)
    if metric is None:
        return _row(h, "unverifiable", reason=f"unknown_metric: {h.metric}")
    matched = [str(d) for d in (getattr(metric, "datasets", None) or []) if str(d)]
    if not matched:
        return _row(h, "unverifiable", reason="no_anchor_dataset")
    time_field = resolve_time_field(semantic_layer, matched, h.metric)
    if not time_field:
        return _row(h, "unverifiable", reason="no_time_field")
    dim_ref = ""
    if h.dimension:
        dim_ref = resolve_dim_ref(semantic_layer, matched, h.dimension) or ""
        if not dim_ref:
            return _row(h, "unverifiable", reason=f"unknown_dimension: {h.dimension}")
    extra_conds: list[dict[str, Any]] = []
    if dim_ref and h.value:
        extra_conds.append({"field": dim_ref, "op": "=", "value": h.value})
    if window is None:
        return _row(h, "unverifiable", reason="no_window")
    if base_window is None:
        return _row(h, "unverifiable", reason="no_baseline_window")
    if not ledger.can(2):
        ledger.yield_("hypothesis", needed=2)
        return _row(h, "unverifiable", reason="query_budget_exceeded")

    cur, status, reason = await _fetch(
        semantic_layer=semantic_layer, runner=runner, datasource=datasource,
        dialect=dialect, metric_name=h.metric, matched=matched, dim_ref=dim_ref,
        extra_conds=extra_conds, time_field=time_field, window=window,
        purpose="hypothesis_current", hypothesis=h.claim,
        ledger=ledger, evidence=evidence)
    if status:
        return _row(h, status, reason=reason, queries=1)
    base, status, reason = await _fetch(
        semantic_layer=semantic_layer, runner=runner, datasource=datasource,
        dialect=dialect, metric_name=h.metric, matched=matched, dim_ref=dim_ref,
        extra_conds=extra_conds, time_field=time_field, window=base_window,
        purpose="hypothesis_baseline", hypothesis=h.claim,
        ledger=ledger, evidence=evidence)
    if status:
        return _row(h, status, reason=reason, queries=1)
    verdict, observed, why = _adjudicate(h, cur, base)
    observed["window"] = list(window)
    observed["base_window"] = list(base_window)
    return _row(h, verdict, observed=observed, reason=why, queries=2)
