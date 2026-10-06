"""``trove validate`` — pre-install dry run over every extension surface.

    trove validate                      # 全部数据源 + 技能,静态检查(零 LLM/零网络)
    trove validate --datasource demo    # 只体检一个数据源
    trove validate --json               # 机器可读(CI 消费)
    trove validate --strict             # 警告也算失败(退出码 1)
    trove validate --live               # 额外连数据源:枚举缺口 + 真方言编译
    trove validate --run --datasource demo
                                        # 装前试跑:本机语料(fixtures)对已确认
                                        # 资产做双态消融(零 LLM/零网络),退出码
                                        # 三分支见下
    trove validate --packs              # + 反作弊:KB 示例 SQL vs 本地 gold 集
    trove validate --packs --gold g.sql # gold 集显式指定(须只体检一个数据源)

Exit codes: 0 clean · 1 hard errors (or any warning under ``--strict``; or,
under ``--run``, a blocking change / assertion failure) · 2 usage — and,
under ``--run``, **无法试跑**(语料缺失/格式错/判定执行出错/一条判定都没跑过)
同样走 2:试跑绝不静默返回 0。The services live in ``trove/services/validate/``
and ``trove/services/extensions/dryrun.py`` — this module is the entry only
(same split as ``scripts/lint_kb.py`` and ``kb/lint.py``).
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
    parser.add_argument(
        "--run", action="store_true",
        help="装前试跑:用本机语料对已确认资产做「装/不装」双态消融"
             "(零 LLM;语料见 --fixtures/--episodes)")
    parser.add_argument(
        "--fixtures", default="auto", metavar="<path|auto>",
        help="试跑语料(--run 时生效):auto(缺省)= .trove/kb/<ds>/fixtures.yml;"
             "或显式 YAML 路径(显式给了却不存在 = 无法试跑,退出码 2)")
    parser.add_argument(
        "--episodes", action="store_true",
        help="试跑语料追加本机 episodes(--run 时生效;跨用户历史问答,只有 SQL"
             "——validator 档如实计 skipped)")
    parser.add_argument(
        "--limit", type=int, default=200, metavar="N",
        help="每个语料源的条数上限(--run 时生效,默认 200)")
    parser.add_argument(
        "--include-questions", action="store_true",
        help="试跑报告中保留问题原文(缺省只出哈希短码 —— 报告可能进 CI 日志)")
    parser.add_argument(
        "--packs", action="store_true",
        help="反作弊检查:KB examples 的 SQL 与本地 gold 集(.trove/kb/<ds>/"
             "gold.sql)的相似度;命中只出警告,绝不自动拒载/改写 KB;"
             "缺省如实报 skipped")
    parser.add_argument(
        "--gold", default="",
        help="gold 集文件(只在该次恰好体检一个数据源时可用;缺省用 "
             ".trove/kb/<ds>/gold.sql)")
    return parser


def main_validate(argv: list[str]) -> int:
    """CLI entry — returns the process exit code."""
    args = _parser().parse_args(argv)
    from trove.services.validate import combined_exit_code, run_validate_with_dryrun

    try:
        report, dry = asyncio.run(run_validate_with_dryrun(
            args.datasource or "", project_root=Path.cwd(), live=args.live,
            packs=args.packs, gold=args.gold,
            run=args.run, fixtures=args.fixtures, episodes=args.episodes,
            limit=args.limit, include_questions=args.include_questions))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130

    # 退出码 = 两支取更严的一支(2 > 1 > 0;服务层 combined_exit_code):
    # 静态干净但试跑无法进行,结论仍是「这次没验成」,不许被静态的 0 盖过去。
    code = combined_exit_code(report, dry, strict=args.strict)

    if args.json:
        data = report.to_dict()
        if dry is not None:
            dry_data = dry.to_dict()
            data["dryrun"] = dry_data
            # 顶层 metrics:scripts/eval_gate.py 的 scorecard 消费面
            # (``score_from_file`` 认顶层 "metrics")—— 试跑报告因此可直接
            # 挂进离线评测回归门,无需任何转换脚本。
            data["metrics"] = dry_data["metrics"]
            data["exit_code"] = code
            data["ok"] = code == 0
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(report.render())
        if dry is not None:
            print()
            print(dry.render())
    return code


# ── REPL slash command ───────────────────────────────────


def register_validate_commands(registry: SlashRegistry, context: dict) -> None:
    """``/validate`` — the same dry run inside the REPL (no exit code)."""

    async def cmd_validate(args: str) -> str:
        """扩展面干跑校验(当前数据源 + 技能)。用法:/validate [--live] [--run [--episodes]]"""
        from trove.services.validate import run_validate

        flags = args.split()
        live = "--live" in flags
        ds = ""
        registry_svc = context.get("connector_registry")
        if registry_svc is not None:
            ds = registry_svc.default_name or ""
        report = await run_validate(
            ds, project_root=Path.cwd(), live=live)
        text = report.render()
        if "--run" in flags:
            # REPL 里没有退出码 —— 试跑结论落在文本末尾的「结论: … → 退出码 N」。
            from trove.services.extensions.dryrun import run_dryrun

            dry = await run_dryrun(
                datasource=ds, project_root=Path.cwd(),
                episodes="--episodes" in flags,
            )
            text += "\n\n" + dry.render()
        return text

    registry.register(SlashCommand(
        name="validate",
        description="Dry-run every extension surface (KB/rules/skills; --run = pre-install corpus dry run). Usage: /validate [--live] [--run [--episodes]]",
        group="system",
        handler=cmd_validate,
    ))
