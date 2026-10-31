#!/usr/bin/env python3
"""把 v0.1 ``semantic_model`` 包装迁移为 OSSIE 0.2 扁平根。

读端对旧包装是硬拒绝(错误文案指向本脚本)。转换本身是纯函数
(``trove.services.kb.ossie_migrate.convert_text``):行式去包装、保留注释与
键序、``version`` 移到最顶、转换后做 YAML 数据等价 + ``parse_ossie`` 回环
两道校验。

用法:
    # dry-run:列出会改哪些文件(默认不写)
    uv run python scripts/migrate_semantics_v02.py --kb-dir .trove/kb --all

    # 迁移仓库内三个 KB + guide snapshot
    uv run python scripts/migrate_semantics_v02.py --kb-dir .trove/kb --all --write
    uv run python scripts/migrate_semantics_v02.py \
        --path scripts/guide_snapshot/root/.trove/kb/demo/semantics.yml \
        --path scripts/guide_snapshot/root/.trove/kb/financial/semantics.yml \
        --write

    # CI/提交前:有漂移或拒绝 → 退出码 1
    uv run python scripts/migrate_semantics_v02.py --kb-dir .trove/kb --all --check

退出码:0 = 无需迁移且全部可读;1 = 有漂移(``--check``)或有文件被拒绝;
2 = 用法/IO 错误。
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

from trove.services.kb.ossie_migrate import MigrationError, convert_text


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--kb-dir", default=".trove/kb",
                    help="KB 根目录(含 <datasource>/semantics.yml;默认 .trove/kb)")
    ap.add_argument("--datasource", action="append", default=[],
                    help="只迁移指定数据源(可重复)")
    ap.add_argument("--all", action="store_true",
                    help="迁移 --kb-dir 下所有含 semantics.yml 的数据源")
    ap.add_argument("--path", action="append", default=[],
                    help="直接指定 semantics.yml 路径(可重复,用于 guide snapshot)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="落盘(默认 dry-run)")
    mode.add_argument("--check", action="store_true",
                      help="只检查;有漂移/拒绝时退出码 1(不落盘)")
    return ap


def _targets(args, ap: argparse.ArgumentParser) -> list[Path]:
    if args.all and args.datasource:
        ap.error("--all 与 --datasource 不能同时使用")
    targets: list[Path] = []
    for raw in args.path:
        targets.append(Path(raw).expanduser())
    kb_dir = Path(args.kb_dir).expanduser()
    if args.all:
        if not kb_dir.is_dir():
            ap.error(f"--kb-dir 不存在: {kb_dir}")
        targets += sorted(kb_dir.glob("*/semantics.yml"))
    elif args.datasource:
        for name in args.datasource:
            targets.append(kb_dir / name / "semantics.yml")
    if not targets:
        ap.error("指定 --all / --datasource / --path 之一")
    return targets


def _write_atomic(path: Path, text: str) -> None:
    mode = path.stat().st_mode
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def main() -> int:
    ap = _parser()
    args = ap.parse_args()
    targets = _targets(args, ap)

    changed = 0
    refused = 0
    for path in targets:
        if not path.is_file():
            print(f"error: 缺文件 {path}", file=sys.stderr)
            return 2
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            print(f"error: 读不动 {path}: {e}", file=sys.stderr)
            return 2
        try:
            new_text, did_change = convert_text(text)
        except MigrationError as e:
            print(f"REFUSED {path}: {e}", file=sys.stderr)
            refused += 1
            continue
        if not did_change:
            print(f"ok      {path}")
            continue
        changed += 1
        print(f"migrate {path}")
        if args.write:
            try:
                _write_atomic(path, new_text)
            except OSError as e:
                print(f"error: 写不动 {path}: {e}", file=sys.stderr)
                return 2
    if args.write:
        print(f"已迁移 {changed} 个文件;拒绝 {refused} 个")
    else:
        print(f"待迁移 {changed} 个文件;拒绝 {refused} 个"
              f"{'(dry-run;加 --write 落盘)' if changed else ''}")
    if refused or (args.check and changed):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
