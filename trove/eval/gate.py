"""离线评测回归门 — baseline vs current 对比,变差拦截(零 LLM/网络)。

每次代码改动后用同一问题集重跑离线评测(record/replay 或 eval_bird),
再与上一次的基线结果对比:准确率指标(ex/编译命中率/检索指标/RRF 参数)
变差到超容差就判定回归,CLI 以非零退出码把改动拦下来。

统一消费两类产物:
- results.jsonl(eval_bird 判定条目,含 compile_meta / path / verdict)
- replay.jsonl(offline_eval record 条目,含 tokens / elapsed_ms / gold_sql)

口径对齐 eval_bird 归因切片:可判定题 = verdict ∈ ``replay.JUDGED_VERDICTS``
(含 REFUSED —— 语义门禁拒绝是已交付的判定,不进分母等于把拒绝率从准确率里
抹掉);EX = MATCH / 可判定;编译命中率 = compile_meta.outcome == "compiled"
的题 / 有 compile_meta 的题。分档 EX 的档位清单 = ``replay.EX_PATH_TIERS``。
集合与档位都只定义一次(在 replay),此处 import 使用 —— 各写一遍必然漂移。
检索/RRF 维度的指标由对应 eval 脚本以 scorecard JSON 输出,同一 compare
逻辑按方向(direction)比较。

方向约定:更高更好(higher)的指标下降即回归;更低更好(lower)的指标
(成本类:token/耗时/重试)上升即回归。容差既支持绝对量也支持相对量。

**分母可见性**(token 均值):``avg_tokens`` 两侧各按"本方有 token 数据的
条目"求均值,覆盖率不同时同一个问题的均值随分母漂移 —— 口径本身不改
(改值会让门与冻结基线错位),但两侧覆盖数(``avg_tokens_n``)随行携带,
覆盖不同时报告行标注 n 并给出⚠未配对告警,配对口径(交集条目)另行列示。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# 判定集合与谓词**不在这里定义** —— 从 replay 取,与 score_replay 共用一份。
# 两处各写一遍的后果不是"重复",是**漂移**:门算出的 ex 与基线钉住的 ex
# 用了不同的分母,红得没有原因,而排查方向会被引到完全错误的地方。
# 2026-10 实测过一次这种漂移:同一份冻结文件,completion 两边算出
# 0.9375 与 0.9062(一个把"SQL 生成了但执行报错"算作完成)。
# (无循环依赖:replay 不 import 任何 trove 模块。)
from trove.eval.replay import EX_PATH_TIERS as _EX_PATH_TIERS
from trove.eval.replay import JUDGED_VERDICTS as _VERDICTS_JUDGED
from trove.eval.replay import completed as _completed
from trove.eval.replay import first_pass as _first_pass
from trove.eval.replay import recovered as _recovered
from trove.eval.replay import self_consistent as _self_consistent
from trove.eval.replay import tried_recovery as _tried
from trove.eval.replay import zero_answer as _zero_answer

#: 更高更好的指标(准确率/覆盖率类)
HIGHER_BETTER = {
    "ex", "compile_hit", "completion", "self_consistency", "first_pass",
    "gold_match", "recovery", "consensus_rate", "avg_confidence",
    "cache_hit_rate",
    "mrr", "recall@k", "ndcg@k",
}
#: 更低更好的指标(成本/失败类)
LOWER_BETTER = {
    "avg_tokens", "total_tokens", "avg_retries", "zero_recall", "zero_answer",
    "avg_elapsed_ms", "total_elapsed_ms",
}

#: **只作分母元信息**的键:计数本身没有"变差"可言,不参与逐指标判定
#: (与 n / n_judged 同规则,进 compare_metrics 的跳过集)。``avg_tokens_n``
#: = token 均值**实际覆盖**的条目数 —— 两侧覆盖不同时均值是分母假象下的
#: 数,2026-10 实测过一次:基线 32 条只有 19 条录了 tokens,gate 报
#: avg_tokens −38%,同 19 题配对口径的真值是 −20.3%。
_COUNT_ONLY_KEYS = frozenset({"n", "n_judged", "avg_tokens_n", "caliber_n"})

#: 同一个 ``avg_tokens_n`` 计数支撑的两个成本指标(均值与总和都只在
#: "有 token 数据的条目"上有意义)—— 行内标注覆盖数时两者都标。
_TOKEN_COVERED_METRICS = ("avg_tokens", "total_tokens")

#: 默认容差:绝对量(rate)或相对量(相对容差以 -r 后缀标记,如 "0.1-r")。
#: ``ex_by_path:*``(补录后 path 覆盖完整才发)不在表内 → 默认容差 "0",
#: 分档样本小,任何下降都值得人看一眼。
DEFAULT_TOLERANCE: dict[str, str] = {
    "ex": "0.01",
    "compile_hit": "0.02",
    "completion": "0.01",
    "self_consistency": "0.01",
    "first_pass": "0.01",
    "zero_answer": "0.02",
    "gold_match": "0.01",
    "recovery": "0.02",
    "consensus_rate": "0.01",
    "avg_confidence": "0.02",
    "mrr": "0.02",
    "recall@k": "0.02",
    "ndcg@k": "0.02",
    # 缓存命中率:新观测,早期录制的缓存计量可能不全 → 相对容差放宽;
    # 抖动期可用 `--ignore cache_hit_rate` 作急停阀(不删指标,先摘门)。
    "cache_hit_rate": "0.20-r",
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
    denominator_notes: list[str] = field(default_factory=list)
    #: 配对口径 token 均值(``paired_token_stats`` 产物;CLI 两侧条目都在手
    #: 时挂上)。None = 无可配对条目 → 报告不渲染这一节。
    token_pairing: dict[str, Any] | None = None

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


def _total_of(entry: dict[str, Any]) -> int:
    """条目录制的 token 总数(未录制/为 0 → 0 = 不进 token 均值分母)。"""
    return int((entry.get("tokens") or {}).get("total") or 0)


def _norm_question(value: Any) -> str:
    """配对键:小写 + 去标点 + 空白压缩(与 ``baseline._norm`` 同一口径,
    由 tests/eval 钉住相等 —— 本地副本的理由同 replay._CACHE_FIELDS:
    对账键两侧写法一旦漂移,配对会静默落到 0 条)。"""
    text = re.sub(r"[\W_]+", " ", str(value or "").lower())
    return re.sub(r"\s+", " ", text).strip()


def metrics_from_entries(entries: Iterable[dict[str, Any]]) -> dict[str, float]:
    """从结果条目计算口径化指标 dict(零 LLM,纯文件数据)。

    兼容 eval_bird results.jsonl 与 offline_eval replay.jsonl 两种条目:
    verdict 键兼容两者的取值;retry 键兼容 retries/retry_count。
    token 指标命中时才发,并随行携带覆盖条目数 ``avg_tokens_n``(见模块
    docstring「分母可见性」—— 值不动,新增的只是分母可见)。
    """
    rows = list(entries)
    n = len(rows)
    judged = [e for e in rows if e.get("verdict") in _VERDICTS_JUDGED]

    # EX:MATCH / 可判定
    matched = sum(1 for e in judged if e.get("verdict") == "MATCH")
    ex = _rate(matched, len(judged))

    # 编译命中率(仅 results.jsonl 有 compile_meta)
    with_meta = [e for e in rows if e.get("compile_meta")]
    compiled = sum(1 for e in with_meta if e["compile_meta"].get("outcome") == "compiled")
    compile_hit = _rate(compiled, len(with_meta))

    # 完成率:两引擎共用 completed()(有 SQL ∧ 判定非硬失败)。旧版这里
    # 内联一份"judged 非报错 + replay 词表 OK"的分支,与 replay 侧各算各的。
    completed_rows = [e for e in rows if _completed(e)]
    completion = _rate(len(completed_rows), n)

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

    # 失败恢复率:统一谓词(与 score_replay 同一份 tried/recovered)
    tried = [e for e in rows if _tried(e)]
    recovered = [e for e in tried if _recovered(e)]
    recovery = _rate(len(recovered), len(tried))

    # 零交付率:无 SQL 且未尝试恢复(这类题原先在 recovery 分子分母都不可见)
    zero_answer = _rate(sum(1 for e in rows if _zero_answer(e)), n)

    # 一次通过率:MATCH ∧ 未触发恢复 / 可判题(无判题不发键)
    first_pass = None
    if judged:
        first_pass = _rate(sum(1 for e in rows if _first_pass(e)), len(judged))

    # 过程自洽率:与 score_replay 同分母(completed),不与 judged 混
    self_consistency = _rate(
        sum(1 for e in rows if _self_consistent(e)), n
    )

    # 共识率与置信度:分母 = completed(与 score_replay 一致)。旧版门用
    # judged、回放用各自的 completed:同一份冻结文件两引擎各算 0.6875 /
    # 0.6667,统一后为今天的 0.6552
    cons = [e for e in completed_rows if e.get("consensus") is True]
    confs = [float(e.get("confidence") or 0.0) for e in completed_rows]

    # token 成本(replay 条目带 tokens 字段;eval_bird 条目走进程级记账)
    totals = [t for t in (_total_of(e) for e in rows) if t > 0]
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
        "self_consistency": round(self_consistency, 4),
        "zero_answer": round(zero_answer, 4),
        "recovery": round(recovery, 4),
        # 与 score_replay 同分母:completed(不是 judged)——门禁的"现值"与
        # 基线的"钉住值"必须同源,否则同一份文件两个数字,红得没有原因
        "consensus_rate": round(_rate(len(cons), len(completed_rows)), 4),
        "avg_confidence": round(sum(confs) / len(confs), 4) if confs else 0.0,
        "avg_retries": round(avg_retries, 3),
        "n": float(n),
        # 可判定题数(EX 分母):gold 失败/崩溃题被平移出分子分母,单独
        # 暴露给门禁做分母稳定性检查——两次对比的样本结构不同,结论
        # 就不完全可比(gate.py 仅告警不拦截,min_n 管样本量下限)。
        "n_judged": float(len(judged)),
    }
    if first_pass is not None:
        metrics["first_pass"] = round(first_pass, 4)
    if gold_match is not None:
        metrics["gold_match"] = round(gold_match, 4)
    if totals:
        metrics["total_tokens"] = float(total_tokens)
        metrics["avg_tokens"] = round(avg_tokens, 1)
        # 均值/总和**只在有 token 数据的条目上有意义** —— 覆盖数随行携带,
        # 口径检查(_check_token_coverage)与行内标注都读它。值本身不动
        # (分母仍是"本方有 token 数据的条目"),新增的只是可见性。
        metrics["avg_tokens_n"] = float(len(totals))
    # 缓存命中率:与 score_replay 走**同一个函数**(import 复用,不是
    # 抄一遍)——门与基线必须同一份口径,这正是本文件开头那段注释的
    # 教训。仅测量过才发键:旧录制无 cache 键 → 不发,冻结基线零位移。
    from trove.eval.replay import cache_hit_stats

    cache_stats = cache_hit_stats(rows)
    if cache_stats is not None:
        metrics["cache_hit_rate"] = round(float(cache_stats["cache_hit_rate"]), 4)
    # 口径拦截率:与 score_replay 走**同一个函数**(import 复用,不抄一遍)。
    # 分母 = 载有 validation_hits 键 且 pred_sql 非空 且 path != "refused"
    # 的行;一行都没测过 → 不发键(旧格式文件零位移)。
    from trove.eval.replay import caliber_block_stats

    caliber = caliber_block_stats(rows)
    if caliber is not None:
        metrics.update(caliber)
    # 墙钟:与 score_replay 同规则(有条目带 elapsed_ms 才发键)。
    # 冻结基线无 elapsed → 不发;补录后自动入基线并被门覆盖。
    elapsed = [int(e.get("elapsed_ms") or 0) for e in rows]
    elapsed = [ms for ms in elapsed if ms > 0]
    if elapsed:
        metrics["total_elapsed_ms"] = float(sum(elapsed))
        metrics["avg_elapsed_ms"] = round(sum(elapsed) / len(elapsed), 1)
    # 分档 EX:与 score_replay 同规则——仅当 path 覆盖完整才发
    # (半份 path 的分档是把"缺数据"当"llm 档");档位清单同样取
    # EX_PATH_TIERS 单一定义,refused 档不许在门这一侧漏掉。
    if judged and all((e.get("path") or "").strip() for e in rows):
        for tier in _EX_PATH_TIERS:
            tier_rows = [
                e for e in rows
                if e.get("path") == tier and e.get("verdict") in _VERDICTS_JUDGED
            ]
            if tier_rows:
                metrics[f"ex_by_path:{tier}"] = round(
                    sum(1 for e in tier_rows if e.get("verdict") == "MATCH") / len(tier_rows), 4
                )
    return metrics


def paired_token_stats(
    baseline_entries: Iterable[dict[str, Any]],
    current_entries: Iterable[dict[str, Any]],
) -> dict[str, Any] | None:
    """配对口径 token 均值:只在**两侧都录制了 token 的交集条目**上算。

    非配对口径的 ``avg_tokens`` 各按"本方有 token 数据的条目"求均值 ——
    覆盖率不同时(基线补录中、本轮记账变全),同一个问题在两侧的分母不同,
    均值随分母漂移。2026-10 实测:基线 19/32 条有 tokens、现值 32/32,
    gate 报 avg_tokens −38%,同 19 题配对真值 −20.3%。两数并存(报告行 +
    ⚠ 口径告警)才让"不可直接比"可见。

    配对键:qid 优先,退归一化问题文本(两侧一组有 qid、一组没有也能对上);
    重复键按出现顺序消费。返回 None = 无可配对条目(报告不渲染这一节)。
    """
    base_tok = [e for e in baseline_entries if _total_of(e) > 0]
    cur_tok = [e for e in current_entries if _total_of(e) > 0]
    if not base_tok or not cur_tok:
        return None

    by_qid: dict[str, list[int]] = {}
    by_question: dict[str, list[int]] = {}
    for pos, entry in enumerate(cur_tok):
        qid = str(entry.get("qid") or "").strip()
        if qid:
            by_qid.setdefault(qid, []).append(pos)
        question = _norm_question(entry.get("question"))
        if question:
            by_question.setdefault(question, []).append(pos)
    taken: set[int] = set()

    def _claim(index: dict[str, list[int]], key: str) -> int | None:
        for pos in index.get(key, ()):
            if pos not in taken:
                taken.add(pos)
                return pos
        return None

    base_sum = cur_sum = n_paired = 0
    for entry in base_tok:
        pos = None
        qid = str(entry.get("qid") or "").strip()
        if qid:
            pos = _claim(by_qid, qid)
        if pos is None:
            question = _norm_question(entry.get("question"))
            if question:
                pos = _claim(by_question, question)
        if pos is None:
            continue
        base_sum += _total_of(entry)
        cur_sum += _total_of(cur_tok[pos])
        n_paired += 1
    if not n_paired:
        return None

    base_avg = base_sum / n_paired
    cur_avg = cur_sum / n_paired
    return {
        "n_paired": n_paired,
        "n_baseline_with_tokens": len(base_tok),
        "n_current_with_tokens": len(cur_tok),
        "n_baseline_unpaired": len(base_tok) - n_paired,
        "n_current_unpaired": len(cur_tok) - n_paired,
        "baseline_avg_tokens": round(base_avg, 1),
        "current_avg_tokens": round(cur_avg, 1),
        "delta_pct": round((cur_avg - base_avg) / base_avg * 100, 1) if base_avg else None,
    }


# ── 对比(纯函数)─────────────────────────────────────────────────


def _direction_of(metric: str) -> str:
    """指标方向:known set 优先,否则按名称启发式(rate 后缀默认 higher)。"""
    if metric in HIGHER_BETTER or metric in LOWER_BETTER:
        return "higher" if metric in HIGHER_BETTER else "lower"
    # 成本/失败/拦截关键词 → lower;其余按名称启发
    if any(k in metric for k in ("token", "elapsed", "retry", "fail", "zero", "cost", "block")):
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
        if metric in ignore or metric in _COUNT_ONLY_KEYS:
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
            note=(
                f"({direction}{'±' + tol_spec if tol else ''})"
                + _row_coverage(metric, baseline, current)
            ),
        ))
    # 只出现在 current 的新指标:无基线不可判,记录但不拦(计数键除外:
    # 它们不参与判定,进 unpaired 只会是人读不懂的噪音)
    for metric in current:
        if metric not in baseline and metric not in ignore and metric not in _COUNT_ONLY_KEYS:
            report.unpaired.append(metric)
    _check_denominator(report, baseline, current)
    _check_token_coverage(report, baseline, current, ignore)
    return report


def _row_coverage(metric: str, baseline: dict[str, float], current: dict[str, float]) -> str:
    """成本均值行内标注两侧覆盖条目数(相同/不可得则不标)。

    报告行是数字被读的地方 —— 分母不同这件事必须在**数字旁边**可见,
    不能只藏在下面的告警段里。
    """
    if metric not in _TOKEN_COVERED_METRICS:
        return ""
    base_n = baseline.get("avg_tokens_n")
    cur_n = current.get("avg_tokens_n")
    if base_n is None or cur_n is None or base_n == cur_n:
        return ""
    return f" [覆盖 n={base_n:.0f}→{cur_n:.0f}]"


#: 可判定题数(EX 分母)的相对漂移阈值:超过即视为样本结构不同,告警不拦截
#: (gold SQL 批量损坏/数据源 schema 变更会导致 GOLD_ERROR 平移分母,
#:  这是对比失真的信号,但门禁不应因样本增减本身而拦)
DENOMINATOR_DRIFT_WARN = 0.20


def _check_denominator(
    report: GateReport,
    baseline: dict[str, float],
    current: dict[str, float],
) -> None:
    """分母稳定性检查:GOLD_ERROR/CRASH 平移 judged 数,样本结构不同时
    指标对比失真——记录告警(render_report / --json 均透出)。"""
    base = baseline.get("n_judged")
    cur = current.get("n_judged")
    if not base or not cur:
        return  # scorecard json / 单侧缺指标 → 无意义
    drift = abs(cur - base) / base
    if drift >= DENOMINATOR_DRIFT_WARN:
        report.denominator_notes.append(
            f"EX 可判定题数漂移 {base:.0f} → {cur:.0f}"
            f"(相对 {drift * 100:.0f}% ≥ {DENOMINATOR_DRIFT_WARN * 100:.0f}%):"
            "样本结构不同,指标对比仅供参考(gold 失败/崩溃平移了分母)"
        )


def _check_token_coverage(
    report: GateReport,
    baseline: dict[str, float],
    current: dict[str, float],
    ignore: set[str],
) -> None:
    """token 均值覆盖检查:两侧纳入均值的条目数不同 → 均值不可直接比。

    均值两侧各自按"本方有 token 数据的条目"求(基线补录中很常见,32 条
    只有 19 条录了 tokens);n 不同即分母不同,同一份数据能读出 −38% 的
    假回归(配对口径真值 −20.3%,2026-10 实测)。只告警不拦截:覆盖缺口
    靠补录收敛,不是代码变差。

    两侧计数都不可得时(如旧 scorecard json 快照)无从判断 —— 不猜、不告警。
    """
    if "avg_tokens" in ignore:
        return
    if "avg_tokens" not in baseline or "avg_tokens" not in current:
        return
    base_n = baseline.get("avg_tokens_n")
    cur_n = current.get("avg_tokens_n")
    if base_n is None or cur_n is None or base_n == cur_n:
        return
    who = (
        f"基线缺 {cur_n - base_n:.0f} 条"
        if base_n < cur_n
        else f"现值缺 {base_n - cur_n:.0f} 条"
    )
    report.denominator_notes.append(
        f"token 均值覆盖不同:基线 n={base_n:.0f} → 现值 n={cur_n:.0f}"
        f"({who},未配对):avg_tokens/total_tokens 两侧不可直接比 —— "
        "均值只在各自有 token 数据的条目上成立;同题配对口径(交集条目)另行列示"
    )


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
    if report.token_pairing:
        tp = report.token_pairing
        delta = tp.get("delta_pct")
        delta_txt = f"{delta:+.1f}%" if delta is not None else "n/a"
        lines.append("")
        lines.append(
            f"配对口径 token 均值(交集 {tp['n_paired']} 题 — 仅两侧都录制了 token 的条目):"
        )
        lines.append(
            f"    {tp['baseline_avg_tokens']} → {tp['current_avg_tokens']}(Δ {delta_txt})"
        )
        if tp["n_baseline_unpaired"] or tp["n_current_unpaired"]:
            lines.append(
                f"    未配对:基线 {tp['n_baseline_unpaired']} 条 / "
                f"现值 {tp['n_current_unpaired']} 条"
            )
    if report.unpaired:
        lines.append("")
        lines.append("无基线不可比: " + ", ".join(sorted(set(report.unpaired))))
    if report.denominator_notes:
        lines.append("")
        lines.append("⚠ 样本结构告警(不拦截):")
        for note in report.denominator_notes:
            lines.append(f"    - {note}")
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
