"""KB lint — 检查 .trove/kb/<datasource> 的已知劣化模式.

静态检查(无需数据库):
  - 术语 mapping 引用不存在的列 / 对 ID 类列求 SUM/AVG
  - 示例 SQL 无法解析 / 引用不存在的表 / 含写操作 / 纯中文问题(英文检索不可达)
  - 列描述为空、lessons pattern 过长或 note 为空

可选实时检查(--datasource):schema_notes 的枚举取值 vs 数据库 DISTINCT 值,
缺值报警(如 loan.status 漏掉 'C')。

Usage:
    uv run python scripts/lint_kb.py [--db-id financial] [--kb-dir DIR]
        [--datasource mysql://root:root@127.0.0.1:3306/financial]

退出码:有 error 级问题为 1,仅有 warning 为 0。
"""

import argparse
import asyncio
import sys
from pathlib import Path

from trove.services.datasource.registry import ConnectorRegistry
from trove.services.datasource.urls import parse_datasource_url
from trove.services.kb.lint import (
    lint_examples,
    lint_lessons,
    lint_tables,
    lint_terms,
)
from trove.services.kb.service import KbService, _parse_file, resolve_kb_root


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-id", default="financial")
    parser.add_argument("--kb-dir", default=None,
                        help="KB 根目录或该数据源的 YAML 目录;默认 <cwd>/.trove/kb")
    parser.add_argument("--datasource", default=None,
                        help="可选:连接数据源,比对枚举取值(如 mysql://root:root@127.0.0.1:3306/financial)")
    return parser.parse_args()


async def check_enums(adapter, table_payloads: dict[str, dict]) -> list[str]:
    """schema_notes 的枚举 vs 数据库 DISTINCT 值,缺值报警。"""
    issues = []
    for table_name, payload in table_payloads.items():
        for col, enum_text in (payload.get("enums") or {}).items():
            known = {
                e.split("=", 1)[0].strip()
                for e in enum_text.split(";") if e.strip()
            }
            if not known:
                continue
            try:
                rows = (await adapter.execute(
                    f"SELECT DISTINCT `{col}` FROM `{table_name}`"
                )).rows
                actual = {str(r[0]) for r in rows if r[0] is not None}
            except Exception as e:
                issues.append(f"枚举探测失败 {table_name}.{col}: {e}")
                continue
            missing = sorted(actual - known)
            if missing:
                issues.append(
                    f"表 {table_name}.{col} 的枚举缺取值: {missing[:10]}"
                    f"{'…' if len(missing) > 10 else ''}")
    return issues


async def check_undocumented_columns(adapter, tables: list[dict]) -> list[str]:
    """数据库实际列 vs schema_notes 已描述列,缺描述的列报警。

    _parse_file 会静默丢弃空描述列,静态检查看不到它们,只能对照
    information_schema(如 district 的 A4~A16 描述被丢光)。
    """
    issues = []
    for table in tables:
        name = str(table["name"])
        documented = set(table.get("columns", {}))
        try:
            rows = (await adapter.execute(
                "SELECT column_name FROM information_schema.columns "
                f"WHERE table_schema = DATABASE() AND table_name = '{name}'"
            )).rows
        except Exception as e:
            issues.append(f"information_schema 查询失败 {name}: {e}")
            continue
        missing = sorted({str(r[0]) for r in rows} - documented)
        if missing:
            issues.append(f"表 {name} 缺列描述: {missing[:10]}"
                          f"{'…' if len(missing) > 10 else ''}")
    return issues


def main() -> int:
    args = parse_args()
    try:
        kb_root = resolve_kb_root(args.kb_dir, args.db_id)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    kb = KbService(Path.cwd(), kb_dir=kb_root)
    ds_dir = kb.kb_dir / args.db_id
    if not ds_dir.is_dir():
        print(f"error: KB 目录不存在: {ds_dir}", file=sys.stderr)
        return 2

    terms, examples, tables, lessons = [], [], [], []
    for yml in sorted(ds_dir.glob("*.yml")):
        for kind, key, payload in _parse_file(yml):
            if kind == "term":
                terms.append(payload)
            elif kind in ("example", "template"):
                examples.append(payload)
            elif kind == "table":
                # payload 不含表名(在 item_key 里),补回 "name" 供 lint 使用
                tables.append({"name": key, **payload})
            elif kind == "lesson":
                lessons.append(payload)

    schema = {
        str(t["name"]): set(t.get("columns", {}))
        for t in tables
    }
    errors = lint_terms(terms, schema) + lint_examples(examples, set(schema))
    warnings = lint_tables(tables) + lint_lessons(lessons)

    if args.datasource:
        async def _live() -> tuple[list[str], list[str]]:
            registry = ConnectorRegistry()
            adapter = await registry.register(
                parse_datasource_url(args.datasource), set_default=True)
            try:
                table_payloads = {str(t["name"]): t for t in tables}
                return (
                    await check_enums(adapter, table_payloads),
                    await check_undocumented_columns(adapter, tables),
                )
            finally:
                await registry.close_all()

        live_errors, live_warnings = asyncio.run(_live())
        errors += live_errors
        warnings += live_warnings

    print(f"== {ds_dir} ==")
    print(f"术语 {len(terms)} | 示例 {len(examples)} | 表 {len(tables)} "
          f"| lessons {len(lessons)}")
    for label, issues in (("ERROR", errors), ("WARN", warnings)):
        if not issues:
            print(f"{label}: 无")
            continue
        print(f"{label} ({len(issues)}):")
        for issue in issues:
            print(f"  - {issue}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
