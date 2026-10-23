"""``trove import semantic`` —— 把 dbt / Cube 语义层导入 Trove。

    trove import semantic --source dbt --path ./dbt --datasource demo
    trove import semantic --source cube --path meta.json --datasource demo
    trove import semantic ... --mode bootstrap     # 空模型:整份写入
    trove import semantic ... --mode stage         # 已有模型:开审批单

默认 ``--mode preview``(零写入)。写盘只走既有确认门:bootstrap 走
``KbService.init_semantics``,stage 走 ``ChangeService.open``(只写变更
记录,``semantics.yml`` 不动),绝不新增第三条写入路径。

Exit codes: 0 clean · 1 运行时/服务错误 · 2 用法错误 / 阻断 lint /
``--strict`` 下有 warning 或 unsupported。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from trove.services.semantic_layer.importers import (
    MODES,
    SOURCES,
    SemanticImportError,
    run_semantic_import,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trove import",
        description="从外部语义层(dbt / Cube)导入声明",
    )
    sub = parser.add_subparsers(dest="action", required=True)
    sem = sub.add_parser(
        "semantic",
        help="导入 dbt Semantic Layer YAML 或 Cube 编译 meta JSON",
    )
    sem.add_argument("--source", required=True, choices=SOURCES,
                     help="dbt = YAML(+可选 manifest);cube = 编译后 meta JSON")
    sem.add_argument("--path", required=True,
                     help="dbt 项目目录/单个 YAML,或 Cube meta JSON 文件")
    sem.add_argument("--datasource", required=True, help="目标数据源名")
    sem.add_argument("--manifest", default=None,
                     help="dbt target/manifest.json(仅 dbt,用于解析 ref/source)")
    sem.add_argument("--mode", default="preview", choices=MODES,
                     help="preview 零写入(默认)/ bootstrap 空模型整份写入 / "
                          "stage 已有模型开审批单")
    sem.add_argument("--force", action="store_true",
                     help="仅 bootstrap:显式覆盖已有非空语义模型")
    sem.add_argument("--note", default="", help="审批单备注(stage)")
    sem.add_argument("--json", action="store_true", help="机器可读输出")
    sem.add_argument("--strict", action="store_true",
                     help="存在 warning / unsupported / 阻断项即退出码 2")
    return parser


def _run(args: argparse.Namespace, mode: str) -> dict:
    return asyncio.run(run_semantic_import(
        Path.cwd(),
        source=args.source,
        path=args.path,
        datasource=args.datasource,
        mode=mode,
        manifest=args.manifest,
        note=args.note,
        force=args.force,
        dialect="mysql",
    ))


def main_import(argv: list[str]) -> int:
    """CLI entry — returns the process exit code."""
    args = _parser().parse_args(argv)
    if args.action != "semantic":
        return 2
    try:
        preflight = _run(args, "preview")
    except SemanticImportError as exc:
        return _fail(args, str(exc), 2, getattr(exc, "blocking", None))
    except Exception as exc:  # noqa: BLE001 — 装配/IO 失败按运行时错误退出
        return _fail(args, f"{type(exc).__name__}: {exc}", 1)

    preflight["mode"] = args.mode
    if preflight.get("blocking"):
        _print_report(preflight, json_output=args.json)
        return 2
    if args.strict and (preflight.get("warnings") or preflight.get("unsupported")):
        preflight["aborted"] = "strict"
        _print_report(preflight, json_output=args.json)
        return 2
    if args.mode == "preview":
        _print_report(preflight, json_output=args.json)
        return 0

    try:
        report = _run(args, args.mode)
    except SemanticImportError as exc:
        return _fail(args, str(exc), 2, getattr(exc, "blocking", None))
    except Exception as exc:  # noqa: BLE001
        return _fail(args, f"{type(exc).__name__}: {exc}", 1)
    _print_report(report, json_output=args.json)
    return 0


def _fail(
    args: argparse.Namespace, message: str, code: int,
    blocking: list[str] | None = None,
) -> int:
    if args.json:
        payload: dict = {"error": message}
        if blocking:
            payload["blocking"] = blocking
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"导入失败: {message}", file=sys.stderr)
        for item in blocking or []:
            print(f"  - {item}", file=sys.stderr)
    return code


def _print_report(report: dict, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    counts = report.get("counts") or {}
    print(
        f"{report.get('source')} 导入({report.get('mode')}): "
        f"datasets {counts.get('datasets', 0)} · fields {counts.get('fields', 0)} · "
        f"metrics {counts.get('metrics', 0)} · "
        f"relationships {counts.get('relationships', 0)}"
    )
    for warning in report.get("warnings") or []:
        print(f"  ! [{warning['code']}] {warning['target']}: {warning['message']}")
    for item in report.get("unsupported") or []:
        print(f"  - [{item['code']}] {item['target']}: {item['message']}")
    blocking = report.get("blocking") or []
    if blocking:
        print("阻断项(未写盘):")
        for item in blocking:
            print(f"  × {item}")
    landed = report.get("landed")
    if landed:
        if landed.get("mode") == "bootstrap":
            print(f"已写入 {landed.get('path')}")
        else:
            verb = "已存在同指纹审批单" if landed.get("duplicate") else "已开审批单"
            print(f"{verb}: {landed.get('change_id')} ({landed.get('path')})")
            print("管理员在语义变更评审里确认 merge 后生效。")
    elif not blocking:
        print(f"建议落点: --mode {report.get('suggestion')}")


__all__ = ["main_import"]
