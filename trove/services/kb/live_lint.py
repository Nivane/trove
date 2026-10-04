"""Live half of KB lint — checks that compare the KB against a *connected*
datasource (``lint.py`` next door stays pure: parsed entries in, issues out).

Two checks, both originally written inside ``scripts/lint_kb.py`` and moved
here so the service side owns them: the script, its callers and ``trove
validate`` now run the same implementation instead of three copies that can
drift apart. ``scripts/lint_kb.py`` re-imports them (its ``check_enums``
import path is pinned by tests).

Both are **opt-in**: they need a live connection, so nothing on the static
path may call them by accident.
"""

from __future__ import annotations

from trove.services.kb.lint import parse_enum_values


async def check_enums(adapter, table_payloads: dict[str, dict]) -> list[str]:
    """schema_notes 的枚举 vs 数据库 DISTINCT 值,缺值报警。"""
    issues = []
    for table_name, payload in table_payloads.items():
        for col, enum_text in (payload.get("enums") or {}).items():
            known = parse_enum_values(enum_text)
            if not known:
                continue
            try:
                rows = (await adapter.execute(
                    f"SELECT DISTINCT `{col}` FROM `{table_name}`"
                )).rows
                # 空串/空白取值是数据噪声,写不出含义,不计入缺口
                actual = {
                    str(r[0]) for r in rows
                    if r[0] is not None and str(r[0]).strip()
                }
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
