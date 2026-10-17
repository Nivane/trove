#!/usr/bin/env python3
"""生成链归因 CLI:历史失败 → L1 结构档位 + L2 责任跳 + loop 三态。

零 LLM / 零网络 / 不连库(设计:~/hub/trove-design/2026-10-16-generation-chain-
attribution-design.md)。退出码:0 正常 / 2 用法或输入错误;无 --strict —
「责任跳桶超阈值即失败」是拍脑袋的门,留到有基线再说。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from trove.eval.attribution import load_rows
from trove.eval.chain import build_report, render_report
from trove.eval.trajectory import (
    load_run_logs, load_structured, load_traces, merge_trajectories,
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="生成链归因:失败记录 → L1 档位 + L2 责任跳(零 LLM 离线)")
    ap.add_argument("--failures", default=".trove/eval/failures.jsonl")
    ap.add_argument("--structured", default="",
                    help="eval_bird 新 records(results.jsonl,含 plan/matched_tables)")
    ap.add_argument("--traces", default="~/.trove/traces.jsonl",
                    help="traces.jsonl 路径;空串关闭")
    ap.add_argument("--runs-dir", default="~/.trove/runs")
    ap.add_argument("--dialect", default="mysql")
    ap.add_argument("--samples", type=int, default=3)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", default="", help="同时把报告写到该文件")
    args = ap.parse_args(argv)

    failures = Path(args.failures).expanduser()
    if not failures.exists():
        print(f"失败记录不存在: {failures}——先跑评测或用 --failures 指定",
              file=sys.stderr)
        return 2
    try:
        rows = load_rows(failures)
    except ValueError as e:
        print(f"读失败记录出错: {e}", file=sys.stderr)
        return 2

    maps = []
    if args.structured:
        try:
            maps.append(load_structured(Path(args.structured).expanduser()))
        except ValueError as e:
            print(f"读结构化 records 出错: {e}", file=sys.stderr)
            return 2
    traces_path = Path(args.traces).expanduser() if args.traces else None
    if traces_path is not None:
        maps.append(load_traces(traces_path))
    maps.append(load_run_logs(Path(args.runs_dir).expanduser()))
    trajs = merge_trajectories(*maps)

    covered = sum(1 for r in rows if r.question.strip() in trajs)
    meta = {
        "failures": str(failures),
        "structured": args.structured or "(off)",
        "traces": str(traces_path) if traces_path else "(off)",
        "runs_dir": str(Path(args.runs_dir).expanduser()),
        "dialect": args.dialect,
        "trajectory_covered_questions": covered,
        "pipeline_note": (
            "轨迹来自 2026-08 两段式管线(planner + 单体 gen_sql);三段式 gen 链"
            "是 2026-09-18 之后。责任跳按当时的节点边界算;L1 档位与节点边界无关。"),
    }
    report = build_report(rows, trajs, dialect=args.dialect, meta=meta,
                          samples=args.samples)
    text = (json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
            if args.json else render_report(report))
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text, end="" if text.endswith("\n") else "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
