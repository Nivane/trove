"""``trove extensions`` —— 扩展资产信封面(设计稿《一切接缝皆契约》E1)。

    trove extensions list                      # 全部资产一封一行(来源/状态/挂点/能力)
    trove extensions list --kind skill         # 只看一类
    trove extensions show no-neg               # 一封的完整体检(挂点 + 推导能力 + 来源链)
    trove extensions --json                    # 机器可读(与 validate --json 同形状)

信封是**编译产物**:kind/name/source/state + 推导出的 capabilities(非作者
声明)+ 节点级挂点 + 来源链 + 推导不出的引用(unresolved,响亮)。读取面与
``trove validate`` 的 envelopes 节同一份(``services/extensions/``),两处
永不漂移。

Exit codes: 0 干净 · 1 show 未命中 · 2 usage。服务在 ``trove/services/
extensions/`` —— 本模块只是入口。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from trove.cli.slash_registry import SlashRegistry, SlashCommand


def _parser() -> argparse.ArgumentParser:
    from trove.services.extensions import KINDS

    parser = argparse.ArgumentParser(
        prog="trove extensions",
        description="扩展资产信封:list 全量一览 / show 单封体检(capabilities 为推导物)",
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
    return parser


def _collect(kind: str = ""):
    """CLI 侧只读装配 —— 与 validate 的 envelopes 节同一条聚合路径。"""
    from trove.services.extensions import collect_assets

    envs = collect_assets(Path.cwd())
    if kind:
        envs = [e for e in envs if e.kind == kind]
    return envs


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


def main_extensions(argv: list[str]) -> int:
    """CLI entry — returns the process exit code."""
    args = _parser().parse_args(argv)

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
