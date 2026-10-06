"""``trove extensions`` —— 扩展资产:信封面(读)与资产包(导出/导入)。

    trove extensions list                      # 全部资产一封一行(来源/状态/挂点/能力)
    trove extensions list --kind skill         # 只看一类
    trove extensions show no-neg               # 一封的完整体检(挂点 + 推导能力 + 来源链)
    trove extensions plan                      # 运行时实际装配清单(disabled 摘除)+ 对账
    trove extensions export my-pack ./my-pack-dir         # 打一个包(只读收集)
    trove extensions import ./my-pack-dir                 # 校验 → 逐条落 pending
    trove extensions import ./my-pack-dir --force         # 同名资产覆盖为 pending 形态

信封是**编译产物**:kind/name/source/state + 推导出的 capabilities(非作者
声明)+ 节点级挂点 + 来源链 + 推导不出的引用(unresolved,响亮)。读取面与
``trove validate`` 的 envelopes 节同一份(``services/extensions/``),两处
永不漂移。

包(``services/extensions/pack.py`` + ``PresetService.export_pack /
import_pack``)的**导入落点一律是 pending**:技能 status 归一、决策落
``decision_drafts.yml``、预设原样落盘但本身不被消费 —— 导入永远等价于
"多了一批待审草稿",绝不绕过管理员确认门。

``plan``(``services/extensions/plan.py``)是**运行时装配清单**:信封 × state
的投影 —— 只有 confirmed 的资产按其挂点展开,disabled(E6 颗粒停用)整封
摘除;并把信封静态视图与运行时读路径(**服务自己的读函数**)对账,差集
非空即漂移。``list`` 是资产目录(全量、含未生效),``plan`` 是"现在到底有
哪些资产会动" —— 与 ``trove validate --impact``(装上会改变什么)互为镜像。

Exit codes: 0 干净 · 1 show 未命中 / 有拒载(冲突/无效,或包级失败:被篡改、
缺文件、``pack_schema`` 过新)/ plan 对账漂移 · 2 usage。服务在
``trove/services/extensions/`` 与 ``trove/services/presets/`` —— 本模块只是入口。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from trove.cli.slash_registry import SlashRegistry, SlashCommand


def _parser() -> argparse.ArgumentParser:
    from trove.services.extensions import KINDS

    parser = argparse.ArgumentParser(
        prog="trove extensions",
        description="扩展资产:信封 list/show(推导能力)+ 资产包 export/import(全落 pending)",
    )
    sub = parser.add_subparsers(dest="action", required=True)

    p_list = sub.add_parser("list", help="列出全部资产信封")
    p_list.add_argument(
        "--kind", default="", choices=("",) + KINDS,
        help="只列某类:skill/decision/preset/term-set/template")
    p_list.add_argument("--json", action="store_true", help="机器可读输出")

    p_show = sub.add_parser("show", help="单封详情(挂点 + 推导能力 + 来源链)")
    p_show.add_argument("name")
    p_show.add_argument("--kind", default="", choices=("",) + KINDS,
                        help="同名跨类时收窄")
    p_show.add_argument("--json", action="store_true", help="机器可读输出")

    p_plan = sub.add_parser(
        "plan", help="运行时实际装配清单(信封 × state;disabled 摘除)+ 对账")
    p_plan.add_argument("--json", action="store_true", help="机器可读输出")

    p_export = sub.add_parser(
        "export", help="把组织资产(skills/decisions/presets)原样打成包")
    p_export.add_argument("name", help="包名(^[a-z0-9][a-z0-9._-]*$)")
    p_export.add_argument("dir", help="目标目录(必须为空或不存在)")
    p_export.add_argument(
        "--origin", default="local",
        help="provenance.origin(local 或 export:<pack>;从别的包导入后再导出时"
             "用来串来源链)")
    p_export.add_argument("--json", action="store_true", help="机器可读输出")

    p_import = sub.add_parser(
        "import", help="校验一个包并逐条落 pending(不绕确认门)")
    p_import.add_argument("dir", help="包目录(含 manifest.yml)")
    p_import.add_argument(
        "--force", action="store_true",
        help="同名资产覆盖为 pending 形态(生效的决策规则永不被覆盖)")
    p_import.add_argument("--json", action="store_true", help="机器可读输出")
    return parser


def _collect(kind: str = ""):
    """CLI 侧只读装配 —— 与 validate 的 envelopes 节同一条聚合路径。"""
    from trove.services.extensions import collect_assets

    envs = collect_assets(Path.cwd())
    if kind:
        envs = [e for e in envs if e.kind == kind]
    return envs


def _services(project_root: Path):
    """CLI 侧最小装配:KB + org skills + preset(与 ``trove preset`` 同一批
    写入口,不另造一份)。"""
    from trove.services.kb.service import KbService
    from trove.services.presets.service import PresetService
    from trove.services.skills.service import SkillService

    kb = KbService(project_root)
    skills = SkillService(project_root / ".trove" / "skills")
    presets = PresetService(
        project_root / ".trove" / "presets", kb=kb, skills=skills)
    return kb, skills, presets


def _caps_line(e) -> str:
    caps = e.capabilities
    parts = []
    if caps.variables:
        parts.append("vars=" + ",".join(caps.variables))
    if caps.effects:
        parts.append("effects=" + ",".join(caps.effects))
    if caps.targets:
        parts.append("targets=" + ",".join(caps.targets))
    return " ".join(parts) or "—"


def _render_env(e) -> str:
    lines = [f"[{e.kind}] {e.name}", f"  来源: {e.source}  状态: {e.state}"]
    if e.provenance.files:
        rev = f"  git={e.provenance.git_rev}" if e.provenance.git_rev else ""
        lines.append(
            f"  文件({len(e.provenance.files)}): sha256={e.provenance.sha256[:12]}…{rev}")
        for f in e.provenance.files:
            lines.append(f"    - {f}")
    if e.mounts:
        lines.append("  挂点:")
        for m in e.mounts:
            lines.append(f"    → {m.node}({m.tier}): {m.effect}")
    lines.append(f"  能力(推导): {_caps_line(e)}")
    for u in e.unresolved:
        lines.append(f"  ! {u}")
    return "\n".join(lines)


def _main_pack(args: argparse.Namespace) -> int:
    """资产包导出/导入分支(B/E5)。"""
    from trove.services.extensions.pack import PackError

    root = Path.cwd()
    try:
        _kb, _skills, presets = _services(root)
    except Exception as exc:  # noqa: BLE001 — 装配失败按一条错误退出
        print(f"装配失败: {exc}", file=sys.stderr)
        return 1

    if args.action == "export":
        try:
            report = presets.export_pack(args.name, Path(args.dir),
                                         origin=args.origin)
        except PackError as exc:
            # 包级拒绝(空包 / 目标非空 / 名或 origin 不合法):什么都没写
            print(f"导出失败: {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        else:
            print(report.render())
        return 0

    # import
    try:
        report = asyncio.run(presets.import_pack(Path(args.dir),
                                                 force=args.force))
    except PackError as exc:
        # 包级拒载(不是包 / 被篡改 / 缺文件 / 版本过新):一条资产都不落
        print(f"导入失败(未落任何资产): {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130

    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(report.render())
    # 拒载条目(冲突/无效)→ 退出码 1:静默的成功码会让 CI 把"部分没导进去"
    # 当成"全导进去了"。
    return 1 if report.refused else 0


def main_extensions(argv: list[str]) -> int:
    """CLI entry — returns the process exit code."""
    args = _parser().parse_args(argv)

    if args.action in ("export", "import"):
        return _main_pack(args)

    if args.action == "plan":
        from trove.services.extensions import build_plan

        try:
            plan = build_plan(Path.cwd())
        except Exception as exc:  # noqa: BLE001 — 装配失败按一条错误退出
            print(f"装配清单失败: {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
        else:
            print(plan.render())
        # 对账漂移 → 退出码 1:静默的成功码会让 CI 把「两份副本已经对不上」
        # 当成「一致」。
        return plan.exit_code

    try:
        envs = _collect(getattr(args, "kind", ""))
    except Exception as exc:  # noqa: BLE001 — 装配失败按一条错误退出
        print(f"信封聚合失败: {exc}", file=sys.stderr)
        return 1

    if args.action == "list":
        if args.json:
            print(json.dumps(
                {"extensions": [e.to_dict() for e in envs]},
                ensure_ascii=False, indent=2))
            return 0
        if not envs:
            print("（没有扩展资产）")
            return 0
        for e in envs:
            mounts = " ".join(f"{m.node}({m.tier})" for m in e.mounts) or "—"
            print(f"{e.kind:9s} {e.name:28s} {e.source:8s} {e.state:10s} "
                  f"{mounts:34s} {_caps_line(e)}")
        return 0

    # show
    hits = [e for e in envs if e.name == args.name]
    if not hits:
        kind_note = f"(kind={args.kind})" if args.kind else ""
        print(f"未找到资产: {args.name}{kind_note}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(
            {"extensions": [e.to_dict() for e in hits]},
            ensure_ascii=False, indent=2))
        return 0
    for i, e in enumerate(hits):
        if i:
            print()
        print(_render_env(e))
    return 0


# ── REPL slash command ───────────────────────────────────


def register_extensions_commands(registry: SlashRegistry, context: dict) -> None:
    """``/extensions`` —— REPL 里的同一份信封(列表 / 单封)。"""

    async def cmd_extensions(args: str) -> str:
        """扩展资产信封:列表或单封详情。用法:/extensions list | /extensions show <name>"""
        tokens = args.split()
        kind = ""
        if "--kind" in tokens:
            i = tokens.index("--kind")
            if i + 1 < len(tokens):
                kind = tokens[i + 1]
                del tokens[i:i + 2]
        try:
            envs = _collect(kind)
        except Exception as exc:  # noqa: BLE001
            return f"信封聚合失败: {exc}"

        if not tokens or tokens[0] == "list":
            if not envs:
                return "（没有扩展资产）"
            lines = []
            for e in envs:
                mounts = " ".join(f"{m.node}({m.tier})" for m in e.mounts) or "—"
                lines.append(
                    f"- [{e.kind}] {e.name} ({e.source}/{e.state}) {mounts} "
                    f"{_caps_line(e)}")
            return "\n".join(lines)

        if tokens[0] == "show":
            if len(tokens) < 2:
                return "用法:/extensions show <name>"
            hits = [e for e in envs if e.name == tokens[1]]
            if not hits:
                return f"未找到资产: {tokens[1]}"
            return "\n\n".join(_render_env(e) for e in hits)

        return "用法:/extensions list | /extensions show <name>"

    registry.register(SlashCommand(
        name="extensions",
        description=("Extension envelopes: list assets / inspect one. "
                     "Usage: /extensions list | /extensions show <name>"),
        group="system",
        handler=cmd_extensions,
    ))
