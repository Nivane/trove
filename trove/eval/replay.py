"""离线 eval 核心——录制轨迹回放打分(零 LLM、零网络)。

对应"Evals"(T2):没有 eval 的 agent 迭代就是裸奔。eval_bird 是成本敏感
的真库全管线;这里是它之外的**低成本回放档**:

- 录制(record):真实 LLM 跑一遍问题集,把每题的 (question, pred_sql,
  row_count, verdict, retries, consensus, tokens, elapsed, gold_sql)
  逐行写入 replay.jsonl——录制一次,之后任意次离线打分。
- 回放(replay):纯函数重放打分——完成率 / 工具正确率(自洽与可选 gold
  精确匹配)/ token 成本 / 失败恢复率,全部确定性、零 LLM 调用。

评分维度对齐面试文档的 eval 四维:任务完成率、工具调用正确率、token
成本、失败恢复率。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable


# ── SQL 归一(离线精确匹配用,零 DB)────────────────────────────


def normalize_sql(sql: str) -> str:
    """SQLGlot 语法归一:解析 → 规范输出;失败退化原文小写空白压缩。"""
    sql = (sql or "").strip()
    if not sql:
        return ""
    try:
        import sqlglot

        return sqlglot.parse_one(sql).sql()
    except Exception:
        return " ".join(sql.lower().split())


def sql_exact_match(pred: str, gold: str) -> bool:
    """结构级精确匹配(pred/gold 解析失败视为不匹配)。"""
    if not pred or not gold:
        return False
    return normalize_sql(pred) == normalize_sql(gold)


# ── 评分(纯函数,确定性,零 LLM/网络/DB)──────────────────────

#: 判定字段的可聚合取值。**这一份是唯一定义**:``gate.metrics_from_entries``
#: 与 ``score_replay`` 都要按同一套口径判 EX,各写一遍必然漂移 —— 而漂移的
#: 后果是 "门算出来的 ex" 与 "基线钉住的 ex" 对不上,红得莫名其妙。
#: 定义放在 replay(被 gate 依赖)而非 gate:gate 已经 import replay。
JUDGED_VERDICTS = {
    "MATCH", "MISMATCH", "GENERATION_ERROR", "EXECUTION_ERROR", "EMPTY_SQL",
    # 语义门禁拒绝是**已交付的判定结果**(反问 + 扩展草稿),不是崩溃:
    # 不判定就等于从分母里抹掉拒绝 —— 拒绝率一升,只要拒绝题不进分母,
    # 执行准确率反而"变好"。冻结基线无 REFUSED 行,此改动对既有基线
    # 逐项零位移(test_scorecard_pin 是机器证明)。
    "REFUSED",
}
#: replay.jsonl 的"跑通"判定(无 DB 执行档,靠自洽)
OK_VERDICTS = {"OK", "MATCH", "EMPTY"}

#: 分档 EX(``ex_by_path:<tier>``)的档位清单。**这一份是唯一定义**:
#: ``score_replay`` 与 ``gate.metrics_from_entries`` 都按它分桶 —— 两处各写
#: 一遍就会漂移,而最容易漂掉的一档恰恰是 ``refused``:拒绝行 path="refused"、
#: verdict∈JUDGED_VERDICTS,却匹配不到任何档 → 拒绝从分档视图里静默消失。
#: (与"REFUSED 必须留在 EX 分母里"是同一件事的两面:拒绝率的变化必须可见。)
#: 顺序即渲染顺序;某档无行时不发键(见 ``score_replay``)。
EX_PATH_TIERS = ("compiled", "partial", "llm", "refused")


def ex_rate(entries: Iterable[dict[str, Any]]) -> tuple[float, int, int]:
    """(命中率, 命中数, 可判题数) —— **执行准确率**,零 DB 聚合。

    ``verdict`` 是**录制当时**执行比对定下的结论,所以它虽然是"结果"字段,
    聚合它却不需要任何数据库或 LLM —— 这正是它该进 CI 门的原因。

    为什么必须有这个指标:``score_replay`` 原有的 completion / self_consistency
    量的是**过程**(跑完没有、候选一致没有),没有任何一项读 ``verdict``。
    结果是:把 8 条 MATCH 改成 MISMATCH,记分卡**一条指标都不动**,门照样
    绿。一个对"对错"瞎的门,不是回归门。

    分母含 GENERATION_ERROR / EXECUTION_ERROR / EMPTY_SQL —— 报错即算错,
    与 ``gate.metrics_from_entries`` 同口径。
    """
    rows = [e for e in entries if e.get("verdict") in JUDGED_VERDICTS]
    hit = sum(1 for e in rows if e.get("verdict") == "MATCH")
    if not rows:
        return 0.0, 0, 0
    return round(hit / len(rows), 4), hit, len(rows)


#: 硬失败判定:即使有 SQL 也不算"完成"(报错即未交付)。跨两个词表 ——
#: results.jsonl 的 EXECUTION_ERROR/GENERATION_ERROR/EMPTY_SQL 与
#: replay.jsonl 的 ERROR/FAIL/REFUSED。MISMATCH **不在**其中:完成 ≠ 正确,
#: 答错也是交付过答案,那是 ex 的事。
HARD_FAIL_VERDICTS = {
    "ERROR", "FAIL", "REFUSED",
    "EXECUTION_ERROR", "GENERATION_ERROR", "EMPTY_SQL",
}


#: 以下谓词是**两引擎共用的唯一口径**(``score_replay`` 与
#: ``gate.metrics_from_entries`` 都必须走这里)。各写一遍的后果不是重复,
#: 是漂移:2026-10 实测两边的 completion 在同一份冻结文件上算出了
#: 0.9375 与 0.9062 —— 一个把"SQL 生成了但执行报错"算作完成,另一个不算。
def completed(e: dict[str, Any]) -> bool:
    """任务完成:有 SQL 且最终判定非硬失败。"""
    return bool(e.get("pred_sql")) and e.get("verdict") not in HARD_FAIL_VERDICTS


def tried_recovery(e: dict[str, Any]) -> bool:
    """本局是否触发过恢复机制(重试/规则拦截/打回)。

    retry 键兼容两种词表:``retry_count``(replay.jsonl)与 ``retries``
    (results.jsonl)—— 只读一个键,另一半题就会漏出恢复率的分母。
    """
    return (
        int(e.get("retry_count") or e.get("retries") or 0) > 0
        or bool(e.get("validation_hits"))
        or bool(e.get("rollback_target"))
        or bool(e.get("fix_mode"))
    )


def recovered(e: dict[str, Any]) -> bool:
    """恢复成功:触发过恢复、最终交付、且既不是错的也不是报错的。

    "触发过恢复"与"恢复成功"是两回事:实测基线 17 次触发只有 3 次以
    MATCH 收场。把两者混为一谈,恢复率就成了"有没有重试过"的同义反复。
    """
    return (
        tried_recovery(e)
        and completed(e)
        and e.get("verdict") != "MISMATCH"
        and not e.get("error")
    )


def self_consistent(e: dict[str, Any]) -> bool:
    """过程自洽:完成 + 共识达成(非平局)且候选池 ≥1。"""
    return (
        completed(e)
        and e.get("consensus") is True
        and int(e.get("n_candidates") or 0) >= 1
    )


def zero_answer(e: dict[str, Any]) -> bool:
    """无答案且从未尝试恢复 —— 终止在"没交付 SQL",恢复机制没被触发。

    这类题原先在恢复率的分子分母里都不可见(基线里 financial-0483/0487
    就是这样):一道没答案的题,恰好因为"没重试"而从指标里消失。
    """
    return not e.get("pred_sql") and not tried_recovery(e)


def first_pass(e: dict[str, Any]) -> bool:
    """一次通过:首答即对(MATCH 且从未触发恢复)。"""
    return e.get("verdict") == "MATCH" and not tried_recovery(e)


#: 缓存字段的本地副本:**replay 不 import 任何 trove 模块**(gate 依赖
#: replay,反向依赖会成环;离线回放要能脱离 runtime 独立跑)。键序与
#: ``trove.llm.token_accounting.CACHE_FIELDS`` 相同,由测试钉住相等 ——
#: 缓存指标跨引擎必须同源,漂移了门与基线就对不上。
_CACHE_FIELDS = (
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "cached_tokens",
)


def _tokens(e: dict[str, Any]) -> dict[str, int]:
    t = e.get("tokens") or {}
    out = {
        "prompt": int(t.get("prompt") or 0),
        "completion": int(t.get("completion") or 0),
        "total": int(t.get("total") or 0),
    }
    for field in _CACHE_FIELDS:
        value = t.get(field)
        if value is not None:  # 键存在 = 录制时测量过(报 0 也留)
            out[field] = int(value)
    return out


def _cache_hit_tokens(bucket: dict[str, int]) -> int | None:
    """命中 token:``cached_tokens`` 优先、退 ``cache_read_input_tokens``
    (同一命中数的两个拼写,取一不求和);都不在 = 未测量 → None。"""
    if bucket.get("cached_tokens") is not None:
        return bucket["cached_tokens"]
    if bucket.get("cache_read_input_tokens") is not None:
        return bucket["cache_read_input_tokens"]
    return None


def cache_hit_stats(entries: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """缓存命中统计(rate = Σ命中/Σprompt,仅算**测量过**的条目)。

    没有任何测量过的条目、或 prompt 总量为 0 → None:不仅发"测不了"的
    0%,与 first_pass「无判题不发键」同一条规则。
    """
    hit_total = 0
    prompt_total = 0
    measured = 0
    for e in entries:
        t = _tokens(e)
        hit = _cache_hit_tokens(t)
        if hit is None:
            continue
        measured += 1
        hit_total += hit
        prompt_total += t["prompt"]
    if not measured or prompt_total <= 0:
        return None
    return {
        "cache_hit_rate": round(hit_total / prompt_total, 4),
        "cache_hit_tokens": hit_total,
        "cache_prompt_tokens": prompt_total,
    }


def score_replay(entries: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """对录制条目集合做离线打分(空集返回全零 + n=0)。

    Returns:
        dict: n / ex(执行准确率) / completion_rate / self_consistency(自洽率) /
        zero_answer(零交付率) / first_pass(一次通过率,有可判题才发) /
        gold_match(若有 gold_sql) / avg_tokens / total_tokens /
        recovery_rate / avg_confidence / consensus_rate / avg_candidates;
        条目带 elapsed_ms / path 时另发 avg_elapsed_ms / total_elapsed_ms /
        ex_by_path;有测量过缓存命中的条目时另发 cache_hit_rate /
        cache_hit_tokens / cache_prompt_tokens。

    ``ex`` 与 ``self_consistency`` 是**两件事**,别混:``self_consistency``
    量的是「生成过程自洽吗」(共识达成、候选池非空),``ex`` 量的是「答案
    对吗」(执行结果与 gold 一致)。过程自洽不等于答案正确 —— 一个高
    consensus 的错答案会让自洽率很好看而 ex 持平。旧名 ``correctness``
    是误称(它从来看的不是正确性),2026-10 改名 self_consistency。
    """
    rows = list(entries)
    n = len(rows)
    if n == 0:
        return {
            "n": 0, "ex": 0.0, "ex_hit": 0, "ex_judged": 0,
            "completion_rate": 0.0, "self_consistency": 0.0,
            "zero_answer": 0.0,
            "gold_match": None, "avg_tokens": 0, "total_tokens": 0,
            "recovery_rate": 0.0, "avg_confidence": 0.0,
            "consensus_rate": 0.0, "avg_candidates": 0.0,
        }

    completed_rows = [e for e in rows if completed(e)]
    self_consist = [e for e in rows if self_consistent(e)]
    # gold 精确匹配(仅存在 gold_sql 的可判题)
    gold_rows = [e for e in rows if (e.get("gold_sql") or "").strip()]
    gold_match = None
    if gold_rows:
        gold_hit = sum(1 for e in gold_rows if sql_exact_match(e.get("pred_sql", ""), e.get("gold_sql", "")))
        gold_match = round(gold_hit / len(gold_rows), 4)

    tried = [e for e in rows if tried_recovery(e)]
    recovered_rows = [e for e in tried if recovered(e)]

    tokens = [_tokens(e) for e in rows]
    total = sum(t["total"] for t in tokens)
    n_with_tokens = sum(1 for t in tokens if t["total"] > 0)

    conf = [float(e.get("confidence") or 0.0) for e in completed_rows]
    cons = [e for e in completed_rows if e.get("consensus") is True]
    cands = [int(e.get("n_candidates") or 0) for e in rows]

    ex, ex_hit, ex_judged = ex_rate(rows)

    out: dict[str, Any] = {
        "n": n,
        "ex": ex, "ex_hit": ex_hit, "ex_judged": ex_judged,
        "completion_rate": round(len(completed_rows) / n, 4),
        "self_consistency": round(len(self_consist) / n, 4),
        "zero_answer": round(sum(1 for e in rows if zero_answer(e)) / n, 4),
        "gold_match": gold_match,
        "gold_n": len(gold_rows),
        "avg_tokens": round(total / n_with_tokens, 1) if n_with_tokens else 0,
        "total_tokens": total,
        "recovery_rate": round(len(recovered_rows) / len(tried), 4) if tried else 0.0,
        "recovery_attempts": len(tried),
        "avg_confidence": round(sum(conf) / len(conf), 4) if conf else 0.0,
        "consensus_rate": round(len(cons) / len(completed_rows), 4) if completed_rows else 0.0,
        "avg_candidates": round(sum(cands) / n, 2),
    }
    # 缓存命中:仅统计**测量过**的条目(旧录制无 cache 键 → 不发键,
    # 冻结基线对门禁逐项零位移);随缓存计量上线的新录制自动带出来。
    cache_stats = cache_hit_stats(rows)
    if cache_stats is not None:
        out.update(cache_stats)
    # 一次通过率只在有可判题时发键:replay.jsonl 的 OK 词表下它恒 0,
    # 发出来是"测不了"冒充"测得是零"。
    if ex_judged:
        out["first_pass"] = round(sum(1 for e in rows if first_pass(e)) / ex_judged, 4)
    elapsed = [int(e.get("elapsed_ms") or 0) for e in rows]
    elapsed = [ms for ms in elapsed if ms > 0]
    if elapsed:
        out["total_elapsed_ms"] = float(sum(elapsed))
        out["avg_elapsed_ms"] = round(sum(elapsed) / len(elapsed), 1)
    # 分档 EX 只在 path 覆盖完整时发:半份 path 的分档是把缺数据当 llm 档。
    # 档位清单 = EX_PATH_TIERS(单一定义,gate 侧 import 同一份)。
    if ex_judged and all((e.get("path") or "").strip() for e in rows):
        by_path: dict[str, float] = {}
        for tier in EX_PATH_TIERS:
            tier_rows = [
                e for e in rows
                if e.get("path") == tier and e.get("verdict") in JUDGED_VERDICTS
            ]
            if tier_rows:
                by_path[tier] = round(
                    sum(1 for e in tier_rows if e.get("verdict") == "MATCH") / len(tier_rows), 4
                )
        if by_path:
            out["ex_by_path"] = by_path
    return out


def scorecard_metrics(score: dict[str, Any]) -> dict[str, float]:
    """``score_replay`` 结果 → 门禁/基线消费的指标快照(键名对齐 gate 口径)。

    抽出来共享是必须的:``offline_eval --scorecard``、CI 重钉、pin 测试
    若各写一遍映射,"钉住的快照"与"现在算的"迟早会用两套键名 —— 而那正是
    unpaired 静默通过、门看着在跑其实瞎了的那条路。
    """
    metrics: dict[str, float] = {
        "ex": score["ex"],
        "completion": score["completion_rate"],
        "self_consistency": score["self_consistency"],
        "recovery": score["recovery_rate"],
        "zero_answer": score["zero_answer"],
        "consensus_rate": score["consensus_rate"],
        "avg_confidence": score["avg_confidence"],
        "avg_tokens": score["avg_tokens"],
        "total_tokens": float(score["total_tokens"]),
        "n": float(score["n"]),
        "n_judged": float(score.get("ex_judged") or 0),
    }
    if score.get("gold_match") is not None:
        metrics["gold_match"] = score["gold_match"]
    if score.get("first_pass") is not None:
        metrics["first_pass"] = score["first_pass"]
    # 缓存命中率只进这个键(tokens 明细留给 score_replay 的输出侧)
    if score.get("cache_hit_rate") is not None:
        metrics["cache_hit_rate"] = float(score["cache_hit_rate"])
    for key in ("avg_elapsed_ms", "total_elapsed_ms"):
        if score.get(key) is not None:
            metrics[key] = float(score[key])
    for tier, value in (score.get("ex_by_path") or {}).items():
        metrics[f"ex_by_path:{tier}"] = float(value)
    return metrics


# ── 录制条目落地 ─────────────────────────────────────────────


def format_entry(
    run_id: str, question: str, *,
    pred_sql: str = "", row_count: int | None = None,
    verdict: str = "", retry_count: int = 0, consensus: bool | None = None,
    confidence: float = 0.0, validation_hits: list | None = None,
    rollback_target: str = "", fix_mode: str = "", n_candidates: int = 0,
    tokens: dict[str, int] | None = None, elapsed_ms: int = 0,
    gold_sql: str = "", kb_hits: list | None = None,
    qid: str = "",
) -> dict[str, Any]:
    """把一次运行折叠成回放条目(录制侧最小契约)。"""
    return {
        "run_id": run_id, "question": question,
        "pred_sql": pred_sql, "row_count": row_count, "verdict": verdict,
        "retry_count": retry_count, "consensus": consensus,
        "confidence": round(confidence, 4),
        "validation_hits": validation_hits or [],
        "rollback_target": rollback_target, "fix_mode": fix_mode,
        "n_candidates": n_candidates, "tokens": tokens or {},
        "elapsed_ms": elapsed_ms, "gold_sql": gold_sql,
        "kb_hits": kb_hits or [], "qid": qid,
    }


def append_entry(path: str | Path, entry: dict[str, Any]) -> None:
    """追加一条录制到 replay.jsonl(目录自动创建,追加幂等)。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load_entries(path: str | Path) -> list[dict[str, Any]]:
    """读取 replay.jsonl(逐行 dict;跳过坏行)。"""
    p = Path(path)
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def render_scorecard(score: dict[str, Any]) -> str:
    """离线打分 → 人类可读 Markdown 记分卡(有数据才出现的行同理不硬凑)。"""
    gm = score.get("gold_match")
    gold_line = f"{gm:.1%}" if gm is not None else "n/a(未提供 gold)"
    lines = [
        f"离线回放记分卡(n={score['n']})",
        f"  执行准确率(EX)          {score['ex']:.1%}"
        f"({score.get('ex_hit', 0)}/{score.get('ex_judged', 0)} 可判题)",
        f"  完成率(completion)     {score['completion_rate']:.1%}",
        f"  自洽率(self_consistency) {score['self_consistency']:.1%}",
    ]
    if "first_pass" in score:
        lines.append(f"  一次通过(first_pass)   {score['first_pass']:.1%}")
    lines += [
        f"  零交付(zero_answer)    {score['zero_answer']:.1%}",
        f"  gold 精确匹配           {gold_line}",
        f"  token 成本              {score['total_tokens']} total / "
        f"{score['avg_tokens']} avg",
    ]
    if score.get("cache_hit_rate") is not None:
        lines.append(
            f"  缓存命中(cache)        {score['cache_hit_rate']:.1%} "
            f"({score.get('cache_hit_tokens', 0)}/"
            f"{score.get('cache_prompt_tokens', 0)} prompt)"
        )
    if "avg_elapsed_ms" in score:
        lines.append(
            f"  墙钟                    {score['total_elapsed_ms']:.0f} ms total / "
            f"{score['avg_elapsed_ms']:.0f} ms avg"
        )
    lines.append(
        f"  失败恢复率(recovery)    {score['recovery_rate']:.1%}"
        f"({score['recovery_attempts']} 次触发)"
    )
    if score.get("ex_by_path"):
        lines.append(
            "  分档 EX(path)           "
            + " / ".join(f"{k} {v:.1%}" for k, v in score["ex_by_path"].items())
        )
    lines.append(
        f"  质量                    conf {score['avg_confidence']:.2f} / "
        f"consensus {score['consensus_rate']:.1%} / "
        f"cands {score['avg_candidates']:.1f}"
    )
    return "\n".join(lines) + "\n"
