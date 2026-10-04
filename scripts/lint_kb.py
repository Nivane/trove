"""KB lint — 检查 .trove/kb/<datasource> 的已知劣化模式.

静态检查(无需数据库):
  - 术语 mapping 引用不存在的列 / 对 ID 类列求 SUM/AVG
  - 示例 SQL 无法解析 / 引用不存在的表 / 含写操作 / 纯中文问题(英文检索不可达)
  - 列描述为空、lessons pattern 过长或 note 为空

可选实时检查(--datasource):schema_notes 的枚举取值 vs 数据库 DISTINCT 值,
缺值报警(如 loan.status 漏掉 'C')。live 检查的实现与静态检查同住
services/kb/(见 live_lint.py),本脚本只是它的壳 —— `trove validate` 走同一份。

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
    lint_semantics_document,
    lint_stats,
    lint_tables,
    lint_terms,
)
# live 检查的实现移到了服务侧(scripts 只是壳);这两个名字在此 re-export,
# ``from scripts.lint_kb import check_enums`` 的既有导入面不变。
from trove.services.kb.live_lint import (  # noqa: F401
    check_enums,
    check_undocumented_columns,
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
    warnings = lint_tables(tables) + lint_lessons(lessons) + lint_stats(tables)

    # 语义层模型(单一真源 semantics.yml):结构/别名/表达式/关系校验。
    # 用文档级包装(与 admin 议题视图、git 提交门禁同一份),否则三处各判
    # 各的字节 —— 顶层错放的 masking/topics 只有包装版看得见。
    semantics_yml = ds_dir / "semantics.yml"
    if semantics_yml.exists():
        try:
            import yaml as _yaml
            _sem_data = _yaml.safe_load(
                semantics_yml.read_text(encoding="utf-8")) or {}
            errors += lint_semantics_document(_sem_data)
        except Exception as e:
            errors.append(f"semantics.yml 读取失败: {e}")

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
