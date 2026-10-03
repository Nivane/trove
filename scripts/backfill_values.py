"""Probe text-column values and backfill them into the semantic model (`values:`).

对数据源的文本列执行 ``SELECT DISTINCT col ... LIMIT 101``（零 LLM、
确定性），把**完整**落在 100 条以内的取值集写进
``.trove/kb/<db>/semantics.yml`` 的字段级 ``values:`` 键 —— 值路由
（"问题里的这个字面量属于哪一列"）要靠这份列取值表。

只**新增**缺失键：已有 ``values`` / ``enum_display`` / ``value_aliases``
的字段一律不动，指标/关系/字段名一律不改（与
``regen_kb_generated.py --write`` 的改名风险隔离）。取值域超过 100 的列
不落库（残缺的取值表会被当成完整词表用，比没有更坏）。

Usage:
    uv run python scripts/backfill_values.py --db-id financial \
        --datasource mysql://root:root@127.0.0.1:3306/financial            # dry-run
    uv run python scripts/backfill_values.py --db-id financial \
        --datasource mysql://root:root@127.0.0.1:3306/financial --write
    uv run python scripts/backfill_values.py --db-id demo --datasource demo --write

======================================================================
!! 警告 — mysql_fin 禁止任何 --write !!
======================================================================
``.trove/kb/mysql_fin`` 是**手工夹具** KB（如 ``loan_count_in_2020``，
其形状 ``COUNT(表.列)`` + 正确 dataset 与生成器产物不可分）。任何自动写入
都可能把人工内容**静默改名/改写**（同 ``scripts/regen_kb_generated.py`` 的
警告）。本脚本对 ``--db-id mysql_fin --write`` 直接**拒绝执行**；dry-run
（只读）可以跑，但请把它当只读工具用。

退出码：0 = 无待写内容（或已写入）；1 = dry-run 有将写内容；2 = 查不成
（KB 目录/数据源/文档往返异常）—— 沿用 check_drift / regen 的约定，
「查不成」不记 0。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv(Path.cwd() / ".env")

from trove.core.types import DatasourceConfig
from trove.services.datasource.registry import ConnectorRegistry
from trove.services.datasource.urls import parse_datasource_url
from trove.services.kb.enum_probe import probe_values
from trove.services.kb.init_pipeline import _backfill_values
from trove.services.kb.lint import is_temporal_field
from trove.services.kb.service import resolve_kb_root

#: 与 regen_kb_generated.py / probe_enums.py 同一套 dump 参数：语义资产
#: 的 YAML 往返 byte-identical 是既成事实（--check 门依赖它），本脚本的
#: diff 只允许出现新增的 ``values:`` 行，所以写盘前先验往返。
_DUMP_KWARGS = dict(default_flow_style=False, allow_unicode=True, sort_keys=False)

#: 不许落盘的数据源（见模块 docstring）。
WRITE_FORBIDDEN = {"mysql_fin"}

#: 内置 demo 的注册名（datasource= 传 ``demo`` 时走内置 SQLite 库）。
DEMO_NAME = "demo"


def parse_args():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db-id", required=True,
                        help="KB 目录名/数据源名（如 financial / demo）")
    parser.add_argument("--datasource", required=True,
                        help="连接串（如 mysql://root:root@127.0.0.1:3306/financial）"
                             "或内置 demo（demo）")
    parser.add_argument("--kb-dir", default=None,
                        help="KB 根目录（含 <db-id>/ 子目录）；默认 <cwd>/.trove/kb")
    parser.add_argument("--max-rows", type=int, default=2_000_000,
                        help="跳过大表(default 2M,与 probe_enums 同护栏)")
    parser.add_argument("--write", action="store_true",
                        help="落盘（默认 dry-run：只打印将新增的字段与条数）")
    return parser.parse_args()


async def _build_registry(datasource: str) -> tuple[ConnectorRegistry, Path | None]:
    """数据源 → 已连接的注册表；内置 demo 建在临时库上（不碰 ~/.trove/demo.db，
    那个文件可能正被另一个会话使用，而这里的探测只需要同一份确定性数据）。"""
    registry = ConnectorRegistry()
    if datasource == DEMO_NAME:
        from trove.demo import create_demo_database
        from trove.services.datasource.adapters.sqlite import SQLiteAdapter

        tmpdir = Path(tempfile.mkdtemp(prefix="trove-demo-probe-"))
        db_path = tmpdir / "demo.db"
        adapter = SQLiteAdapter(name=DEMO_NAME, config={"path": str(db_path)})
        await adapter.connect()
        try:
            await create_demo_database(adapter)
        finally:
            await adapter.disconnect()
        await registry.register(
            DatasourceConfig(
                name=DEMO_NAME, type="sqlite",
                connection_params={"path": str(db_path)}, default=True,
            ),
            set_default=True,
        )
        return registry, tmpdir
    await registry.register(parse_datasource_url(datasource), set_default=True)
    return registry, None


def _covered_with_vocabulary(
    doc: dict, probed: dict[str, dict[str, list[str]]],
) -> list[str]:
    """探测到、但**已有**值词表(或时间字段)因此不会被写的字段 —— 只用于
    报告(说明"探到了但没动",免得 review 时以为探测漏了)。"""
    names: list[str] = []
    for entry in doc.get("semantic_model", []) or []:
        if not isinstance(entry, dict):
            continue
        for dataset in entry.get("datasets", []) or []:
            if not isinstance(dataset, dict):
                continue
            columns = probed.get(str(dataset.get("name", ""))) or {}
            for fld in dataset.get("fields", []) or []:
                if not isinstance(fld, dict):
                    continue
                if str(fld.get("name", "")) not in columns:
                    continue
                has_vocab = (
                    "values" in fld or bool(fld.get("enum_display"))
                    or bool((fld.get("ai_context") or {}).get("value_aliases"))
                )
                if has_vocab or is_temporal_field(fld):
                    names.append(f"{dataset.get('name')}.{fld.get('name')}")
    return names


async def _run(args) -> int:
    if args.db_id in WRITE_FORBIDDEN and args.write:
        print(
            f"拒绝执行:--db-id {args.db_id} 是手工夹具 KB,禁止任何 --write"
            "（自动写入会静默改写人工内容；见脚本 docstring）。",
            file=sys.stderr,
        )
        return 2

    try:
        kb_root = resolve_kb_root(args.kb_dir, args.db_id)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    root = kb_root if kb_root is not None else Path.cwd() / ".trove" / "kb"
    semantics_path = root / args.db_id / "semantics.yml"
    if not semantics_path.exists():
        print(f"error: {semantics_path} 不存在（先 /kb init 生成语义模型）",
              file=sys.stderr)
        return 2

    registry, tmpdir = await _build_registry(args.datasource)
    try:
        schema = await registry.get_schema()
        probed = await probe_values(registry, schema, max_rows=args.max_rows)
    finally:
        await registry.close_all()
        if tmpdir is not None:
            import shutil

            shutil.rmtree(tmpdir, ignore_errors=True)

    raw = semantics_path.read_text(encoding="utf-8")
    doc = yaml.safe_load(raw) or {}
    if yaml.safe_dump(doc, **_DUMP_KWARGS) != raw:
        print(
            f"error: {semantics_path} YAML 往返非 byte-identical，拒绝重写"
            "（重写会把格式化差异混进 diff）", file=sys.stderr)
        return 2

    # 先算报告用的"探到但不动"清单,再做回填(回填就地改 doc)。
    untouched = _covered_with_vocabulary(doc, probed)
    added = _backfill_values(doc, probed)

    probed_cols = sum(len(cols) for cols in probed.values())
    print(f"== {semantics_path} ==")
    print(f"探测: {len(probed)} 张表 / {probed_cols} 列取值域完整落在 100 以内")
    if untouched:
        preview = "、".join(untouched[:10]) + ("…" if len(untouched) > 10 else "")
        print(f"已有值词表/时间字段，不动 {len(untouched)} 个: {preview}")
    if not added:
        print("无待新增 values:（已同步）")
        return 0
    print(f"将新增 values: {len(added)} 个字段")
    for name, count in added.items():
        print(f"  {name} → {count} 条")
    if not args.write:
        print("(dry-run;加 --write 落盘)")
        return 1
    semantics_path.write_text(yaml.safe_dump(doc, **_DUMP_KWARGS), encoding="utf-8")
    print(f"已写入 {semantics_path}")
    return 0


def main() -> int:
    return asyncio.run(_run(parse_args()))


if __name__ == "__main__":
    sys.exit(main())
