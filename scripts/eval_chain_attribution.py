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


_DEFAULT_TRACES = "~/.trove/traces.jsonl"
_DEFAULT_RUNS = "~/.trove/runs"


def _warn_missing(flag: str, path: Path, what: str) -> None:
    """显式给的输入路径不存在:按空源继续,但在 stderr 说一句。

    默认路径(fresh 机器上可能本来就没有 ``~/.trove/runs``)保持静默——不能
    因为一份可选的旁路输入不存在就让默认调用失败;但「显式拼错」与「本来就
    没有」在报告里原本不可区分,一句 stderr 让它们分得开。
    """
    print(f"{flag} 指定的{what}不存在: {path}(按空源继续)", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="生成链归因:失败记录 → L1 档位 + L2 责任跳(零 LLM 离线)")
    ap.add_argument("--failures", default=".trove/eval/failures.jsonl")
    ap.add_argument("--structured", default="",
                    help="eval_bird 新 records(results.jsonl,含 plan/matched_tables)")
    ap.add_argument("--traces", default=None,
                    help=f"traces.jsonl 路径;默认 {_DEFAULT_TRACES},空串关闭")
    ap.add_argument("--runs-dir", default=None,
                    help=f"运行日志目录;默认 {_DEFAULT_RUNS}")
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
    except (ValueError, OSError) as e:
        print(f"读失败记录出错: {e}", file=sys.stderr)
        return 2

    maps = []
    if args.structured:
        structured = Path(args.structured).expanduser()
        if not structured.exists():
            _warn_missing("--structured", structured, "文件")
        try:
            maps.append(load_structured(structured))
        except (ValueError, OSError) as e:
            print(f"读结构化 records 出错: {e}", file=sys.stderr)
            return 2

    # 显式与默认要分得开:默认 traces 在 fresh 机器上不存在是常态(静默),
    # 显式给的路径不存在则多半是笔误(stderr 一句,仍按空源继续)。
    traces_explicit = args.traces is not None
    if args.traces == "":
        traces_path = None
    else:
        traces_path = Path(args.traces or _DEFAULT_TRACES).expanduser()
    if traces_path is not None:
        if traces_explicit and not traces_path.exists():
            _warn_missing("--traces", traces_path, "文件")
        try:
            maps.append(load_traces(traces_path))
        except OSError as e:
            print(f"读 traces 出错: {e}", file=sys.stderr)
            return 2

    runs_dir = Path(args.runs_dir or _DEFAULT_RUNS).expanduser()
    if args.runs_dir is not None and not runs_dir.is_dir():
        _warn_missing("--runs-dir", runs_dir, "目录")
    try:
        maps.append(load_run_logs(runs_dir))
    except OSError as e:
        print(f"读运行日志出错: {e}", file=sys.stderr)
        return 2
    trajs = merge_trajectories(*maps)

    # 按**题**计,不按行:同一题的多条失败行只算一次(报告另给行/题双计数,
    # 覆盖数若按行会随重试次数虚高)。
    covered = len({r.question.strip() for r in rows
                   if r.question.strip() in trajs})
    meta = {
        "failures": str(failures),
        "structured": args.structured or "(off)",
        "traces": str(traces_path) if traces_path else "(off)",
        "runs_dir": str(runs_dir),
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
    # 一处收尾:文件与 stdout 逐字节同(JSON 不以换行结尾,补一个)。
    payload = text if text.endswith("\n") else text + "\n"
    if args.out:
        try:
            Path(args.out).expanduser().write_text(payload, encoding="utf-8")
        except OSError as e:
            print(f"写报告失败: {e}", file=sys.stderr)
            return 2
    print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
