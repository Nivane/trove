#!/usr/bin/env python3
"""基线完整性检查 CLI — 可复现基线产物的健康门(零 LLM/网络)。

确认 eval/baseline/ 的两个产物可对账、指标可计算、无冗余/重复:
  - questions.jsonl 问题集字段完整、qid 稳定
  - results.jsonl 能算指标、覆盖问题集(缺题默认仅警告)
  - qid 无"问题集外"与"重复判定"(硬问题)

默认只要求基线可解析;--require-full 要求问题集全覆盖(收基线 / CI
严格执行用)。退出码 0 = 健康,1 = 硬问题(或 --require-full 下覆盖不全),
2 = 用法错误。

用法:
  uv run python scripts/eval_baseline.py check
  uv run python scripts/eval_baseline.py check --require-full
  uv run python scripts/eval_baseline.py check \
      --questions eval/baseline/questions.jsonl --results .trove/eval/results.jsonl
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from trove.eval.baseline import check_integrity

_DEFAULT_DIR = Path("eval") / "baseline"


def parse_args() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    chk = sub.add_parser("check", help="基线完整性检查")
    chk.add_argument("--questions", default=str(_DEFAULT_DIR / "questions.jsonl"))
    chk.add_argument("--results", default=str(_DEFAULT_DIR / "results.jsonl"))
    chk.add_argument("--require-full", action="store_true",
                     help="缺题结果视为硬问题(收基线/CI 严格档)")
    chk.add_argument("--json", action="store_true", help="只输出 JSON 判定")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.cmd != "check":
        print(f"error: unknown cmd {args.cmd}", file=sys.stderr)
        return 2

    report = check_integrity(
        args.questions, args.results, require_full=args.require_full,
    )
    if args.json:
        import json

        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ok"] else 1

    print(f"基线完整性 · {Path(args.questions).name} × {Path(args.results).name} "
          f"({report['n_questions']} 题 / {report['n_results']} 条结果, "
          f"覆盖 {report['coverage']:.0%})")
    for w in report["warnings"]:
        print(f"  ! {w}")
    for p in report["problems"]:
        print(f"  ✗ {p}")
    if report["ok"]:
        print("✓ 健康")
    else:
        print("✗ 存在硬问题")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
