#!/usr/bin/env python3
"""Compile hit-rate 聚合器:从 eval results.jsonl 统计语义层编译闭环。

口径(与 eval_bird.attribution_slices 一致):
- 可判定题 = verdict ∈ ``trove.eval.replay.JUDGED_VERDICTS``(MATCH / MISMATCH /
  GENERATION_ERROR / EXECUTION_ERROR / EMPTY_SQL / REFUSED;gold 失败/崩溃题
  不进分母)—— 集合从 replay import,不给私有副本:副本漂移过一次(漏 REFUSED,
  docstring 却自称与 attribution_slices 一致),拒绝率因此从分母里静默消失;
- hit-rate = compile_meta.outcome == "compiled" 的题数 / 有 compile_meta
  的题数(无 meta 的题 = 编译决策从未发生,单独计数);
- MISS 分因按 miss_reason 聚合(计数、占 MISS 比、该原因下 EX%);
- 路径对比:path ∈ ``EX_PATH_TIERS``(compiled / partial / llm / refused)的 EX%,
  空档省略 —— 与 score_replay 的 ``ex_by_path`` 同源同规则。

纯文件 IO,零网络零 key;可对任意历史 results.jsonl 直接跑。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from trove.eval.replay import EX_PATH_TIERS, JUDGED_VERDICTS


def load_entries(path: Path, question_filter: str = "") -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if not path.exists():
        return entries
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue  # 容忍半行/坏行(评测中断的常见残留)
        if question_filter and question_filter not in str(entry.get("question") or ""):
            continue
        entries.append(entry)
    return entries


def _rate(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "0/0 (0.0%)"
    m = sum(1 for r in rows if r.get("verdict") == "MATCH")
    return f"{m}/{len(rows)} ({m / len(rows) * 100:.1f}%)"


def stats(entries: list[dict[str, Any]], verbose: bool = False) -> list[str]:
    judged = [e for e in entries if e.get("verdict") in JUDGED_VERDICTS]
    with_meta = [e for e in judged if e.get("compile_meta")]
    compiled = [e for e in with_meta if e["compile_meta"].get("outcome") == "compiled"]
    missed = [e for e in with_meta if e["compile_meta"].get("outcome") == "miss"]

    lines: list[str] = [
        f"总条目: {len(entries)}(可判定 {len(judged)})",
        f"有 compile_meta: {len(with_meta)}"
        f"(无 meta = 编译决策未发生,如 query_sketch 无计划)",
        f"编译命中率: {len(compiled)}/{len(with_meta)}"
        f" ({len(compiled) / len(with_meta) * 100:.1f}%)"
        if with_meta else
        "编译命中率: 无 compile_meta 数据(语义层未接线?)",
    ]
    # 路径 EX%:档位清单复用 EX_PATH_TIERS(与 ex_by_path 分档同源);
    # 无行入档的空档省略(与 score_replay 的"空档不发键"同一规则)。
    tier_rates: list[str] = []
    for tier in EX_PATH_TIERS:
        tier_rows = [e for e in judged if e.get("path") == tier]
        if tier_rows:
            tier_rates.append(f"{tier} {_rate(tier_rows)}")
    lines.append(
        "路径 EX%: " + (" | ".join(tier_rates) if tier_rates else "(无 path 数据)")
    )

    if missed:
        lines.append("--- MISS 分因 ---")
        by_reason: dict[str, list[dict[str, Any]]] = {}
        for e in missed:
            by_reason.setdefault(
                str(e["compile_meta"].get("miss_reason") or "unknown"), []).append(e)
        for reason, rows in sorted(
            by_reason.items(), key=lambda kv: -len(kv[1]),
        ):
            lines.append(
                f"{reason}: {len(rows)} ({len(rows) / len(missed) * 100:.1f}% "
                f"of MISS) EX {_rate(rows)}")
            if verbose:
                for e in rows[:3]:
                    lines.append(
                        f"    - [{e.get('run_id')}] {str(e.get('question'))[:60]}"
                        f" :: {e['compile_meta'].get('miss_component')}")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", default=".trove/eval/results.jsonl",
                        help="results.jsonl 路径(默认 .trove/eval/results.jsonl)")
    parser.add_argument("--question", default="",
                        help="只统计问题文本含该子串的条目")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="每个 MISS 原因打印 3 条样本 run_id/component")
    args = parser.parse_args()

    entries = load_entries(Path(args.results), args.question)
    if not entries:
        print(f"no entries in {args.results}")
        return 0
    for line in stats(entries, verbose=args.verbose):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
