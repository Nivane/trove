#!/usr/bin/env python3
"""离线评测回归门 CLI — baseline vs current 对比,变差拦截(零 LLM/网络)。

每次代码改动后重跑离线评测(replay/eval_bird/检索/RRF),与基线对比:

  uv run python scripts/eval_gate.py \
      --baseline .trove/eval/results.baseline.jsonl \
      --current  .trove/eval/results.jsonl \
      --tol ex=0.02 --tol compile_hit=0.03

输入文件:
- results.jsonl(eval_bird 判定条目)→ 自动算 EX/编译命中率/完成率/恢复率/token
- replay.jsonl(offline_eval record)→ 另算 gold 精确匹配
- scorecard json(检索/RRF 脚本 --scorecard 产物,含 "metrics" 键)→ 原样对比

退出码:
- 0: 全部指标在容差内(或无回归)
- 1: 存在回归(变差的被拦下来)
- 2: 用法/输入错误

常用容差(--tol metric=val,val 可为 "0.05" 绝对量或 "0.10-r" 相对量):
  ex / compile_hit / gold_match / mrr / recall@k ...
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from trove.eval.gate import (
    GateReport,
    compare_metrics,
    render_report,
    score_from_file,
)


def parse_args() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--baseline", required=True, help="基线结果文件(results/replay/scorecard)")
    p.add_argument("--current", required=True, help="本次结果文件(同上)")
    p.add_argument("--tol", action="append", default=[],
                   help="覆盖单指标容差,如 --tol ex=0.02(可多次)")
    p.add_argument("--ignore", action="append", default=[],
                   help="跳过判定的指标,如 --ignore avg_tokens(可多次)")
    p.add_argument("--min-n", type=int, default=0,
                   help="当前结果样本量下限(低于则视为数据不足,报错退出)")
    p.add_argument("--json", action="store_true",
                   help="只输出 JSON 判定结果(便于 CI 解析)")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    baseline = score_from_file(args.baseline)
    current = score_from_file(args.current)

    if not baseline and not current:
        print(f"error: 两个文件都没有可解析的指标 — baseline={args.baseline} current={args.current}",
              file=sys.stderr)
        return 2

    n_cur = int(current.get("n", 0))
    if args.min_n and n_cur < args.min_n:
        print(f"error: 当前样本量 {n_cur} < --min-n {args.min_n}(数据不足,不能下结论)",
              file=sys.stderr)
        return 2

    tolerances: dict[str, str] = {}
    for item in args.tol:
        key, _, val = item.partition("=")
        tolerances[key] = val

    report: GateReport = compare_metrics(
        baseline, current,
        tolerances=tolerances,
        ignore=set(args.ignore),
    )
    report.baseline_label = Path(args.baseline).name
    report.current_label = Path(args.current).name

    if args.json:
        print(json.dumps({
            "baseline": args.baseline,
            "current": args.current,
            "passed": report.passed,
            "regressions": [
                {
                    "metric": m.metric,
                    "baseline": m.baseline,
                    "current": m.current,
                    "delta": m.delta,
                    "direction": m.direction,
                }
                for m in report.regressions
            ],
            "metrics": [
                {
                    "metric": m.metric,
                    "baseline": m.baseline,
                    "current": m.current,
                    "ok": m.ok,
                }
                for m in report.metrics
            ],
        }, ensure_ascii=False, indent=2))
        return 0 if report.passed else 1

    print(render_report(report))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
