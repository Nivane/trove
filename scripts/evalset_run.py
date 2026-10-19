#!/usr/bin/env python3
"""手动 evalset 运行器(薄壳):等价于 ``trove evalset run ...``。

Usage:
    uv run python scripts/evalset_run.py --datasource demo
    uv run python scripts/evalset_run.py --datasource demo --tier all
    uv run python scripts/evalset_run.py --datasource demo --compile-only
    uv run python scripts/evalset_run.py --datasource demo --yes

参数与 ``trove evalset run`` 完全一致;本脚本只做参数解析与转调,
退出码原样透传(0 通过 / 1 未达标 / 2 环境或参数问题)。成本护栏同源:
没有 ``--yes`` 时运行器只做 dry-run,不会调用 LLM。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from trove.cli.commands.evalset_cmds import main_evalset


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="evalset_run.py",
        description="运行某数据源的验收集(等价于 trove evalset run)",
    )
    parser.add_argument(
        "--datasource", required=True,
        help=(
            "目标数据源(demo 或 .trove/datasources.yml 中的注册名;"
            "不接受 scheme:// URL)"
        ),
    )
    parser.add_argument(
        "--project-root", type=Path, default=Path.cwd(),
        help="项目根(默认当前目录)",
    )
    parser.add_argument(
        "--tier", default="verified",
        help="verified / semantic / synthetic / all(依次跑三个 tier)",
    )
    parser.add_argument("--limit", type=int, default=0, help="最多跑 N 题(0 = 全部)")
    parser.add_argument("--sample", type=int, default=0, help="稳定抽样 N 题")
    parser.add_argument(
        "--compile-only", action="store_true",
        help="只做 SQLGlot + 试执行自检(零 LLM)",
    )
    parser.add_argument("--yes", action="store_true", help="确认调用 LLM(正式运行)")
    parser.add_argument(
        "--strict", action="store_true",
        help="reference_errors/leakage_suspect > 0 时退出码 2",
    )
    parser.add_argument("--lang", default="zh", help="问题语言(默认 zh)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    # 用法错误(缺 --datasource、非法参数)在脚本层就以 2 退出;
    # 校验通过后原样转交给 ``trove evalset run`` 的同一条路径。
    _build_parser().parse_args(args)
    return main_evalset(["run", *args])


if __name__ == "__main__":
    raise SystemExit(main())
