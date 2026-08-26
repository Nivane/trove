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


def _completed(e: dict[str, Any]) -> bool:
    """任务完成:有 SQL 且最终判定非硬失败。"""
    return bool(e.get("pred_sql")) and e.get("verdict") not in ("ERROR", "FAIL", "REFUSED")


def _tried_recovery(e: dict[str, Any]) -> bool:
    """本局是否触发过恢复机制(重试/规则拦截/打回)。"""
    return (
        int(e.get("retry_count") or 0) > 0
        or bool(e.get("validation_hits"))
        or bool(e.get("rollback_target"))
        or bool(e.get("fix_mode"))
    )


def _tokens(e: dict[str, Any]) -> dict[str, int]:
    t = e.get("tokens") or {}
    return {
        "prompt": int(t.get("prompt") or 0),
        "completion": int(t.get("completion") or 0),
        "total": int(t.get("total") or 0),
    }


def score_replay(entries: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """对录制条目集合做离线打分(空集返回全零 + n=0)。

    Returns:
        dict: n / completion_rate / correctness(自洽) / gold_match(若有
        gold_sql) / avg_tokens / total_tokens / recovery_rate /
        avg_confidence / consensus_rate / avg_candidates。
    """
    rows = list(entries)
    n = len(rows)
    if n == 0:
        return {
            "n": 0, "completion_rate": 0.0, "correctness": 0.0,
            "gold_match": None, "avg_tokens": 0, "total_tokens": 0,
            "recovery_rate": 0.0, "avg_confidence": 0.0,
            "consensus_rate": 0.0, "avg_candidates": 0.0,
        }

    completed = [e for e in rows if _completed(e)]
    # 自洽正确:完成 + 共识达成(非平局)且候选池≥1
    self_consistent = [
        e for e in completed if e.get("consensus") is True and int(e.get("n_candidates") or 0) >= 1
    ]
    # gold 精确匹配(仅存在 gold_sql 的可判题)
    gold_rows = [e for e in rows if (e.get("gold_sql") or "").strip()]
    gold_match = None
    if gold_rows:
        gold_hit = sum(1 for e in gold_rows if sql_exact_match(e.get("pred_sql", ""), e.get("gold_sql", "")))
        gold_match = round(gold_hit / len(gold_rows), 4)

    tried = [e for e in rows if _tried_recovery(e)]
    recovered = [e for e in tried if _completed(e)]

    tokens = [_tokens(e) for e in rows]
    total = sum(t["total"] for t in tokens)
    n_with_tokens = sum(1 for t in tokens if t["total"] > 0)

    conf = [float(e.get("confidence") or 0.0) for e in completed]
    cons = [e for e in completed if e.get("consensus") is True]
    cands = [int(e.get("n_candidates") or 0) for e in rows]

    return {
        "n": n,
        "completion_rate": round(len(completed) / n, 4),
        "correctness": round(len(self_consistent) / n, 4),
        "gold_match": gold_match,
        "gold_n": len(gold_rows),
        "avg_tokens": round(total / n_with_tokens, 1) if n_with_tokens else 0,
        "total_tokens": total,
        "recovery_rate": round(len(recovered) / len(tried), 4) if tried else 0.0,
        "recovery_attempts": len(tried),
        "avg_confidence": round(sum(conf) / len(conf), 4) if conf else 0.0,
        "consensus_rate": round(len(cons) / len(completed), 4) if completed else 0.0,
        "avg_candidates": round(sum(cands) / n, 2),
    }


# ── 录制条目落地 ─────────────────────────────────────────────


def format_entry(
    run_id: str, question: str, *,
    pred_sql: str = "", row_count: int | None = None,
    verdict: str = "", retry_count: int = 0, consensus: bool | None = None,
    confidence: float = 0.0, validation_hits: list | None = None,
    rollback_target: str = "", fix_mode: str = "", n_candidates: int = 0,
    tokens: dict[str, int] | None = None, elapsed_ms: int = 0,
    gold_sql: str = "", kb_hits: list | None = None,
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
        "kb_hits": kb_hits or [],
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
    """离线打分 → 人类可读 Markdown 记分卡。"""
    gm = score.get("gold_match")
    gold_line = f"{gm:.1%}" if gm is not None else "n/a(未提供 gold)"
    return (
        f"离线回放记分卡(n={score['n']})\n"
        f"  完成率(completion)     {score['completion_rate']:.1%}\n"
        f"  自洽正确率(correctness) {score['correctness']:.1%}\n"
        f"  gold 精确匹配           {gold_line}\n"
        f"  token 成本              {score['total_tokens']} total / "
        f"{score['avg_tokens']} avg\n"
        f"  失败恢复率(recovery)    {score['recovery_rate']:.1%}"
        f"({score['recovery_attempts']} 次触发)\n"
        f"  质量                    conf {score['avg_confidence']:.2f} / "
        f"consensus {score['consensus_rate']:.1%} / "
        f"cands {score['avg_candidates']:.1f}\n"
    )
