"""离线评测回归门 — baseline vs current 对比,变差拦截(零 LLM/网络)。

每次代码改动后用同一问题集重跑离线评测(record/replay 或 eval_bird),
再与上一次的基线结果对比:准确率指标(ex/编译命中率/检索指标/RRF 参数)
变差到超容差就判定回归,CLI 以非零退出码把改动拦下来。

统一消费两类产物:
- results.jsonl(eval_bird 判定条目,含 compile_meta / path / verdict)
- replay.jsonl(offline_eval record 条目,含 tokens / elapsed_ms / gold_sql)

口径对齐 eval_bird 归因切片:可判定题 = verdict ∈ {MATCH, MISMATCH,
GENERATION_ERROR, EXECUTION_ERROR, EMPTY_SQL};EX = MATCH / 可判定;编译
命中率 = compile_meta.outcome == "compiled" 的题 / 有 compile_meta 的题。
检索/RRF 维度的指标由对应 eval 脚本以 scorecard JSON 输出,同一 compare
逻辑按方向(direction)比较。

方向约定:更高更好(higher)的指标下降即回归;更低更好(lower)的指标
(成本类:token/耗时/重试)上升即回归。容差既支持绝对量也支持相对量。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

#: 可判定题 verdict 集合(与 eval_bird 归因切片一致;gold 失败/崩溃题不计)
_VERDICTS_JUDGED = {
    "MATCH", "MISMATCH", "GENERATION_ERROR", "EXECUTION_ERROR", "EMPTY_SQL",
}
#: replay.jsonl 的完成判定(离线档无 DB 执行,靠自洽)
_VERDICTS_OK = {"OK", "MATCH", "EMPTY"}

#: 更高更好的指标(准确率/覆盖率类)
HIGHER_BETTER = {
    "ex", "compile_hit", "completion", "correctness", "gold_match", "recovery",
    "consensus_rate", "avg_confidence", "mrr", "recall@k", "ndcg@k",
}
#: 更低更好的指标(成本/失败类)
LOWER_BETTER = {
    "avg_tokens", "total_tokens", "avg_retries", "zero_recall",
    "avg_elapsed_ms", "total_elapsed_ms",
}

#: 默认容差:绝对量(rate)或相对量(相对容差以 -r 后缀标记,如 "0.1-r")
DEFAULT_TOLERANCE: dict[str, str] = {
    "ex": "0.01",
    "compile_hit": "0.02",
    "completion": "0.01",
    "correctness": "0.01",
    "gold_match": "0.01",
    "recovery": "0.02",
    "consensus_rate": "0.01",
    "avg_confidence": "0.02",
    "mrr": "0.02",
    "recall@k": "0.02",
    "ndcg@k": "0.02",
    "avg_tokens": "0.10-r",
    "total_tokens": "0.10-r",
    "avg_retries": "0.20",
    "zero_recall": "0.02",
    "avg_elapsed_ms": "0.10-r",
    "total_elapsed_ms": "0.10-r",
}


@dataclass
class MetricResult:
    """单个指标的对比结果:方向 / 基线值 / 现值 / 容差 / 是否回归。"""

    metric: str
    baseline: float | None
    current: float | None
    tolerance: str = "0"
    direction: str = "higher"
    ok: bool = True
    delta: float = 0.0
    note: str = ""


@dataclass
class GateReport:
    """一次门禁对比的完整结果。"""

    baseline_label: str = ""
    current_label: str = ""
    metrics: list[MetricResult] = field(default_factory=list)
    unpaired: list[str] = field(default_factory=list)

    @property
    def regressions(self) -> list[MetricResult]:
        return [m for m in self.metrics if not m.ok]

    @property
    def passed(self) -> bool:
        return not self.regressions


# ── 指标计算(纯函数)─────────────────────────────────────────────


def _num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _rate(numer: int, denom: int) -> float:
    return numer / denom if denom else 0.0


def metrics_from_entries(entries: Iterable[dict[str, Any]]) -> dict[str, float]:
    """从结果条目计算口径化指标 dict(零 LLM,纯文件数据)。

    兼容 eval_bird results.jsonl 与 offline_eval replay.jsonl 两种条目:
    verdict 键兼容两者的取值;retry 键兼容 retries/retry_count。
    """
    rows = list(entries)
    n = len(rows)
    judged = [e for e in rows if e.get("verdict") in _VERDICTS_JUDGED]
    judged_set = {id(e) for e in judged}

    # EX:MATCH / 可判定;完成率口径对 replay 退化(verdict=OK 且非 ERROR)
    matched = sum(1 for e in judged if e.get("verdict") == "MATCH")
    ex = _rate(matched, len(judged))

    # 编译命中率(仅 results.jsonl 有 compile_meta)
    with_meta = [e for e in rows if e.get("compile_meta")]
    compiled = sum(1 for e in with_meta if e["compile_meta"].get("outcome") == "compiled")
    compile_hit = _rate(compiled, len(with_meta))

    # 完成率:judged 内非 hard-fail(EXECUTION_ERROR/GENERATION_ERROR 之外)
    completed = [
        e for e in judged
        if e.get("verdict") not in ("EXECUTION_ERROR", "GENERATION_ERROR", "EMPTY_SQL")
    ]
    # replay 条目(非 MATCH/MISMATCH 体系)按 OK 判定完成
    replay_completed = [
        e for e in rows
        if id(e) not in judged_set and e.get("verdict") in _VERDICTS_OK
        and not e.get("error")
    ]
    completion = _rate(len(completed) + len(replay_completed), n)

    # gold 精确匹配(仅 replay 条目带 gold_sql;零 DB 结构归一)
    from trove.eval.replay import sql_exact_match

    gold_rows = [e for e in rows if (e.get("gold_sql") or "").strip()]
    gold_match = None
    if gold_rows:
        gold_match = _rate(
            sum(
                1 for e in gold_rows
                if sql_exact_match(e.get("pred_sql", ""), e.get("gold_sql", ""))
            ),
            len(gold_rows),
        )

    # 失败恢复率(与 replay._tried_recovery 同口径)
    def _tried(e: dict[str, Any]) -> bool:
        return (
            int(e.get("retry_count") or e.get("retries") or 0) > 0
            or bool(e.get("validation_hits"))
            or bool(e.get("rollback_target"))
            or bool(e.get("fix_mode"))
        )

    tried = [e for e in rows if _tried(e)]
    recovered = [e for e in tried if e.get("verdict") in _VERDICTS_JUDGED
                 and e.get("verdict") != "MISMATCH"
                 and not e.get("error")]
    recovery = _rate(len(recovered), len(tried))

    # 共识率与置信度
    cons = [e for e in judged if e.get("consensus") is True]
    confs = [_num(e.get("confidence")) for e in judged]
    confs = [c for c in confs if c is not None]

    # token 成本(replay 条目带 tokens 字段;eval_bird 条目走进程级记账)
    totals = [
        int((e.get("tokens") or {}).get("total") or 0)
        for e in rows
        if (e.get("tokens") or {}).get("total")
    ]
    total_tokens = sum(totals)
    avg_tokens = _rate(total_tokens, len(totals))

    # 重试次数
    retries = [
        int(e.get("retry_count") or e.get("retries") or 0)
        for e in rows
    ]
    avg_retries = _rate(sum(retries), len(retries))

    metrics: dict[str, float] = {
        "ex": round(ex, 4),
        "compile_hit": round(compile_hit, 4),
        "completion": round(completion, 4),
        "recovery": round(recovery, 4),
        "consensus_rate": _rate(len(cons), len(judged)),
        "avg_retries": round(avg_retries, 3),
        "n": float(n),
    }
    if gold_match is not None:
        metrics["gold_match"] = round(gold_match, 4)
    if confs:
        metrics["avg_confidence"] = round(sum(confs) / len(confs), 4)
    if totals:
        metrics["total_tokens"] = float(total_tokens)
        metrics["avg_tokens"] = round(avg_tokens, 1)
    return metrics


# ── 对比(纯函数)─────────────────────────────────────────────────


def _direction_of(metric: str) -> str:
    """指标方向:known set 优先,否则按名称启发式(rate 后缀默认 higher)。"""
    if metric in HIGHER_BETTER or metric in LOWER_BETTER:
        return "higher" if metric in HIGHER_BETTER else "lower"
    # 成本/失败关键词 → lower;其余按名称启发
    if any(k in metric for k in ("token", "elapsed", "retry", "fail", "zero", "cost")):
        return "lower"
    return "higher"


def _tolerance_of(metric: str, overrides: dict[str, str] | None = None) -> str:
    return (overrides or {}).get(metric, DEFAULT_TOLERANCE.get(metric, "0"))


def compare_metrics(
    baseline: dict[str, float],
    current: dict[str, float],
    tolerances: dict[str, str] | None = None,
    ignore: set[str] | None = None,
) -> GateReport:
    """对比两组指标,按方向与容差判定每个指标是否回归。

    tolerance 格式:"0.05"(绝对值)或 "0.10-r"(相对基线)。ignore 里的
    指标跳过不判定。只出现在一方的指标进 unpaired(数量级不同,不可比)。
    """
    ignore = ignore or set()
    report = GateReport()
    for metric, base in sorted(baseline.items()):
        if metric in ignore or metric == "n":
            continue
        cur = current.get(metric)
        if cur is None:
            report.unpaired.append(metric)
            continue
        direction = _direction_of(metric)
        tol_spec = _tolerance_of(metric, tolerances)
        relative = tol_spec.endswith("-r")
        tol = float(tol_spec[:-2] or 0) if relative else float(tol_spec or 0)
        delta = cur - base
        threshold = base * tol if relative else tol
        if direction == "higher":
            ok = delta >= -threshold
        else:
            ok = delta <= threshold
        report.metrics.append(MetricResult(
            metric=metric, baseline=base, current=cur,
            tolerance=tol_spec, direction=direction, ok=ok,
            delta=round(delta, 4),
            note=f"({direction}{'±' + tol_spec if tol else ''})",
        ))
    # 只出现在 current 的新指标:无基线不可判,记录但不拦
    for metric in current:
        if metric not in baseline and metric not in ignore:
            report.unpaired.append(metric)
    return report


def render_report(report: GateReport) -> str:
    """把对比结果渲染成人类可读的 Markdown 记分卡。"""
    lines = [f"## 回归门禁 · {report.baseline_label} → {report.current_label}"]
    if not report.metrics:
        lines.append("(无可比指标 — 双方都为空或已全部 ignore)")
        return "\n".join(lines)
    header = f"{'指标':<18} {'基线':>10} {'现值':>10} {'Δ':>10} 判定"
    lines.append(header)
    lines.append("-" * len(header))
    for m in report.metrics:
        flag = "OK" if m.ok else "REGRESS"
        lines.append(
            f"{m.metric:<18} {m.baseline:>10.4f} {m.current:>10.4f} "
            f"{m.delta:>+10.4f} {flag:>7} {m.note}"
        )
    if report.unpaired:
        lines.append("")
        lines.append("无基线不可比: " + ", ".join(sorted(set(report.unpaired))))
    lines.append("")
    if report.passed:
        lines.append(f"✅ 通过 — {len(report.metrics)} 项指标无回归")
    else:
        lines.append(f"❌ 拦截 — {len(report.regressions)} 项指标回归:")
        for m in report.regressions:
            lines.append(f"    - {m.metric}: {m.baseline:.4f} → {m.current:.4f}")
    return "\n".join(lines)


# ── 文件 IO ──────────────────────────────────────────────────────


def load_entries(path: str | Path) -> list[dict[str, Any]]:
    """读取结果 jsonl(兼容 results.jsonl / replay.jsonl / scorecard json)。"""
    p = Path(path)
    if not p.exists():
        return []
    text = p.read_text(encoding="utf-8")
    if p.suffix == ".json":
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            return []
        if isinstance(obj, dict) and "metrics" in obj:
            return [obj]
        if isinstance(obj, list):
            return [e for e in obj if isinstance(e, dict)]
        return [obj]
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def score_from_file(path: str | Path) -> dict[str, float]:
    """从文件直接算指标:results/replay jsonl → metrics_from_entries;
    scorecard json → 原样返回其指标(检索/RRF 脚本 --scorecard 产物)。"""
    entries = load_entries(path)
    if not entries:
        return {}
    if isinstance(entries[0], dict) and "metrics" in entries[0]:
        m = entries[0].get("metrics") or {}
        return {k: float(v) for k, v in m.items() if _num(v) is not None}
    return metrics_from_entries(entries)
