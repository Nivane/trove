"""``trove extensions`` —— 扩展资产包的导出与导入。

    trove extensions export my-pack ./my-pack-dir         # 打一个包(只读收集)
    trove extensions import ./my-pack-dir                 # 校验 → 逐条落 pending
    trove extensions import ./my-pack-dir --force         # 同名资产覆盖为 pending 形态
    trove extensions export my-pack ./out --origin export:upstream-pack

一个"包"= 目录 + ``manifest.yml``(形态与校验链在
``trove/services/extensions/pack.py``;资产策略在
``PresetService.export_pack / import_pack``)。

**导入的落点一律是 pending**:技能 status 归一、决策落
``decision_drafts.yml``、预设原样落盘但本身不被消费 —— 导入永远等价于
"多了一批待审草稿",绝不绕过管理员确认门。冲撞策略(默认拒载点名 /
``--force`` 覆盖为 pending / 生效规则永不被覆盖 / 幂等重放报 skipped)
见 ``PresetService`` 的包一节 docstring。

Exit codes: 0 clean · 1 有拒载(冲突/无效,或包级失败:被篡改、缺文件、
``pack_schema`` 过新)· 2 usage。服务在 ``trove/services/extensions/``
与 ``trove/services/presets/`` —— 本模块只是入口。

注:本文件与 E1 车道的 ``trove extensions`` 入口在各分支上并存,合流时
以加法式合并为准 —— 这里只放 pack 子命令,不放别的。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trove extensions",
        description="扩展资产包:导出(org 技能/决策/预设)与导入(全部落 pending)",
    )
    sub = parser.add_subparsers(dest="action", required=True)

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


def main_extensions(argv: list[str]) -> int:
    """CLI entry — returns the process exit code."""
    args = _parser().parse_args(argv)
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
