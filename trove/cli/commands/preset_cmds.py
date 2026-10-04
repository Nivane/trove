"""``trove preset`` —— 预设包(preset)的查看与套用。

    trove preset list                        # 内置 + 组织两份来源,标注遮蔽
    trove preset show financial-analysis     # 逐段摘要 + 原文
    trove preset apply financial-analysis --datasource demo
                                             # 套用:全部内容落 pending 草稿

套用的红线(与仓规同源):产出**只有草稿** —— 技能草稿走 org skill 的确认门,
决策规则草稿落在 ``decision_drafts.yml``(不在 decisions.yml 里,执行面结构性
地读不到),主题域草稿走语义层审批流。逐条确认后才生效。

Exit codes: 0 clean · 1 preset 不存在/套用失败(或 ``--strict`` 下有 unresolved)·
2 usage。服务在 ``trove/services/presets/`` —— 本模块只是入口。
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
        prog="trove preset",
        description="预设包(接入模板):列表 / 查看 / 套用(套用只落 pending 草稿)",
    )
    sub = parser.add_subparsers(dest="action", required=True)

    p_list = sub.add_parser("list", help="列出内置 + 组织预设")
    p_list.add_argument("--json", action="store_true", help="机器可读输出")

    p_show = sub.add_parser("show", help="查看一份预设(逐段摘要 + 原文)")
    p_show.add_argument("name")
    p_show.add_argument("--json", action="store_true", help="机器可读输出")

    p_apply = sub.add_parser(
        "apply", help="套用到数据源 —— 全部内容落 pending 草稿,确认后才生效")
    p_apply.add_argument("name")
    p_apply.add_argument("--datasource", required=True, help="目标数据源名")
    p_apply.add_argument("--json", action="store_true", help="机器可读输出")
    p_apply.add_argument(
        "--strict", action="store_true",
        help="有任何 unresolved(引用解析不到)即退出码 1")
    return parser


def _services(project_root: Path):
    """CLI 侧最小装配:KB + org skills + preset(与 ``trove serve`` 同一批
    写入口,不另造一份)。"""
    from trove.services.kb.service import KbService
    from trove.services.presets.service import PresetService
    from trove.services.skills.service import SkillService

    kb = KbService(project_root)
    skills = SkillService(project_root / ".trove" / "skills")
    presets = PresetService(
        project_root / ".trove" / "presets", kb=kb, skills=skills)
    return kb, skills, presets


def main_preset(argv: list[str]) -> int:
    """CLI entry — returns the process exit code."""
    args = _parser().parse_args(argv)
    root = Path.cwd()
    try:
        _kb, _skills, presets = _services(root)
    except Exception as exc:  # noqa: BLE001 — 装配失败按一条错误退出
        print(f"装配失败: {exc}", file=sys.stderr)
        return 1

    if args.action == "list":
        entries = presets.merged()
        if args.json:
            print(json.dumps({"presets": entries}, ensure_ascii=False, indent=2))
        elif not entries:
            print("（没有可用预设:内置目录 trove/presets 与 .trove/presets 都是空的）")
        else:
            for e in entries:
                tag = f"[{e['source']}]"
                if e.get("shadowed"):
                    tag += "（被组织版遮蔽）"
                counts = e.get("counts") or {}
                detail = " · ".join(f"{k} {v}" for k, v in counts.items() if v)
                print(f"{e['name']} v{e['version']} {tag} {detail}")
                if e.get("description"):
                    print(f"    {e['description']}")
        return 0

    if args.action == "show":
        try:
            preset = presets.load(args.name)
            raw = presets.read_raw(args.name)
        except KeyError as exc:
            print(str(exc).strip("'"), file=sys.stderr)
            return 1
        except Exception as exc:  # noqa: BLE001
            print(f"preset 不合法: {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps({
                "name": preset.name, "version": preset.version,
                "description": preset.description, "author": preset.author,
                "source": preset.source, "counts": preset.counts, "text": raw,
            }, ensure_ascii=False, indent=2))
            return 0
        print(f"{preset.name} v{preset.version}（来源: {preset.source}）")
        print(f"  {preset.description}")
        counts = " · ".join(f"{k} {v}" for k, v in preset.counts.items() if v)
        print(f"  条目: {counts or '（空）'}")
        print()
        print(raw)
        return 0

    # apply
    from trove.services.presets.models import PresetError

    try:
        report = asyncio.run(
            presets.apply(args.name, args.datasource))
    except KeyError as exc:
        print(str(exc).strip("'"), file=sys.stderr)
        return 1
    except PresetError as exc:
        print(f"preset 不合法: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130

    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(report.render())
    if args.strict and report.counts.get("unresolved", 0):
        return 1
    return 0


# ── REPL slash command ───────────────────────────────────


def register_preset_commands(registry: SlashRegistry, context: dict) -> None:
    """``/preset`` —— REPL 里的同一套动作(列表 / 套用到当前数据源)。"""

    async def cmd_preset(args: str) -> str:
        """预设包:列表或套用。用法:/preset list | /preset apply <name>"""
        tokens = args.split()
        project_root = Path.cwd()
        try:
            _kb, _skills, presets = _services(project_root)
        except Exception as exc:  # noqa: BLE001
            return f"预设装配失败: {exc}"

        if not tokens or tokens[0] == "list":
            entries = presets.merged()
            if not entries:
                return "（没有可用预设）"
            lines = []
            for e in entries:
                mark = "（被组织版遮蔽）" if e.get("shadowed") else ""
                lines.append(f"- {e['name']} v{e['version']} [{e['source']}]{mark}")
            return "\n".join(lines)

        if tokens[0] == "apply":
            if len(tokens) < 2:
                return "用法:/preset apply <name>（套用到当前数据源）"
            registry_svc = context.get("connector_registry")
            ds = getattr(registry_svc, "default_name", "") or ""
            if not ds:
                return "当前没有默认数据源 —— 先 /datasource 选一个"
            try:
                report = await presets.apply(tokens[1], ds)
            except KeyError as exc:
                return str(exc).strip("'")
            except Exception as exc:  # noqa: BLE001
                return f"套用失败: {exc}"
            return report.render()

        return "用法:/preset list | /preset apply <name>"

    registry.register(SlashCommand(
        name="preset",
        description="预设包(接入模板):列表 / 套用。用法:/preset list | /preset apply <name>",
        group="system",
        handler=cmd_preset,
    ))
