"""``trove validate`` — pre-install dry run over every extension surface.

    trove validate                      # 全部数据源 + 技能,静态检查(零 LLM/零网络)
    trove validate --datasource demo    # 只体检一个数据源
    trove validate --json               # 机器可读(CI 消费)
    trove validate --strict             # 警告也算失败(退出码 1)
    trove validate --live               # 额外连数据源:枚举缺口 + 真方言编译

Exit codes: 0 clean · 1 hard errors (or any warning under ``--strict``) ·
2 usage. The service lives in ``trove/services/validate/`` — this module is
the entry only (same split as ``scripts/lint_kb.py`` and ``kb/lint.py``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from trove.cli.slash_registry import SlashRegistry, SlashCommand


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trove validate",
        description="扩展面干跑校验:KB lint / 决策规则编译 / org 技能挂点与冲突",
    )
    parser.add_argument(
        "--datasource", default="",
        help="只体检该数据源(缺省 = .trove/kb 下全部);技能面始终全量")
    parser.add_argument(
        "--json", action="store_true", help="机器可读输出(整份报告)")
    parser.add_argument(
        "--strict", action="store_true", help="警告也视为失败(退出码 1)")
    parser.add_argument(
        "--live", action="store_true",
        help="额外连接数据源:枚举缺口探测 + 用真实方言编译决策规则"
             "(缺省关闭,不联网)")
    return parser


def main_validate(argv: list[str]) -> int:
    """CLI entry — returns the process exit code."""
    args = _parser().parse_args(argv)
    from trove.services.validate import run_validate

    try:
        report = asyncio.run(run_validate(
            args.datasource or "", project_root=Path.cwd(), live=args.live))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130

    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(report.render())
    return report.exit_code(strict=args.strict)


# ── REPL slash command ───────────────────────────────────


def register_validate_commands(registry: SlashRegistry, context: dict) -> None:
    """``/validate`` — the same dry run inside the REPL (no exit code)."""

    async def cmd_validate(args: str) -> str:
        """扩展面干跑校验(当前数据源 + 技能)。用法:/validate [--live]"""
        from trove.services.validate import run_validate

        flags = args.split()
        live = "--live" in flags
        ds = ""
        registry_svc = context.get("connector_registry")
        if registry_svc is not None:
            ds = registry_svc.default_name or ""
        report = await run_validate(
            ds, project_root=Path.cwd(), live=live)
        return report.render()

    registry.register(SlashCommand(
        name="validate",
        description="Dry-run every extension surface (KB/rules/skills). Usage: /validate [--live]",
        group="system",
        handler=cmd_validate,
    ))
