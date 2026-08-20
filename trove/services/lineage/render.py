"""Lineage answer rendering — deterministic, no LLM, i18n-aware.

Takes LineageService retrieval results and turns them into readable
Markdown/context sections. Pure: callers fetch the facts, this module
formats them.
"""

from __future__ import annotations

from typing import Any

from trove.core.i18n import L


def _fmt_kind(kind: str, lang: str) -> str:
    return {
        "create_view": L(lang, "视图", "view"),
        "create_table_as": L(lang, "表", "table"),
        "query": L(lang, "查询", "query"),
    }.get(kind, kind)


def render_table_upstream(
    datasource: str, table: str, producers: list[dict[str, Any]], lang: str = "zh",
) -> str:
    """Markdown section: producers of a table."""
    head = L(lang, f"表 `{table}` 的上游（生产者）", f"Upstream of `{table}` (producers)")
    if not producers:
        return f"**{head}**: " + L(lang, "无可记录的血缘定义。", "no recorded lineage definitions.") 
    lines = [f"**{head}**: {len(producers)}"]
    for p in producers:
        base = "、".join(p.get("tables_read", []))
        base_txt = (
            L(lang, f"｜基表 {base}", f"｜base {base}") if base else ""
        )
        lines.append(
            f"- {_fmt_kind(p['kind'], lang)} `{p['name']}` {base_txt}"
            f"（{L(lang, '产出列', 'outputs')}: {', '.join(o['name'] for o in p['outputs']) or '-'}）"
        )
    return "\n".join(lines)


def render_column_lineage(
    table: str, column: str, lineage: dict[str, Any], lang: str = "zh",
) -> str:
    """Markdown section: how table.column is produced and consumed."""
    cell = f"{table}.{column}"
    producers, consumers = lineage.get("producers", []), lineage.get("consumers", [])
    parts = [f"### {L(lang, f'{cell} 的数据血缘', f'Lineage of {cell}')}\n"]

    if producers:
        for p in producers:
            sources = p.get("sources", [])
            src_txt = "、".join(
                f"{s['table']}.{s['column']}" if s.get("table") else s.get("column", "")
                for s in sources
            ) or "-"
            parts.append(
                f"- {_fmt_kind(p['kind'], lang)} `{p['name']}`"
                f"{L(lang, ' 按', ' computed via')} "
                f"`{', '.join(s.get('expr') for s in sources) or p['sql'][:80]}`"
                f"{L(lang, '，取自', ' from ')}{src_txt}"
            )
        parts.append("")
    else:
        parts.append(
            L(
                lang,
                f"未找到 `{cell}` 的产生式定义（视图/ETL 未收录，或字段来自物理基表）。",
                f"No producing definition found for `{cell}` (no ingested view/ETL, or the field lives on a physical base table).",
            )
        )
        parts.append("")

    if consumers:
        parts.append(L(lang, "被以下查询消费：", "Consumed by:"))
        for c in consumers[:8]:
            parts.append(f"- `{c['sql'][:90]}`" + (f"（{c['runs']} 次）" if c.get("runs") else ""))
    else:
        parts.append(L(lang, "尚无已记录的消费查询。", "Nothing has consumed it yet."))
    return "\n".join(parts)


def render_table_downstream(
    table: str, consumers: list[dict[str, Any]], lang: str = "zh",
) -> str:
    """Markdown section: consumers of a table."""
    if not consumers:
        return L(lang, f"`{table}` 暂无下游消费者。", f"`{table}` has no downstream consumers.")
    parts = [L(lang, f"`{table}` 的下游：", f"Downstream of `{table}`:")]
    for c in consumers:
        if c.get("name"):
            parts.append(f"- {_fmt_kind(c['kind'], lang)} `{c['name']}`")
        else:
            parts.append(f"- `{c['sql'][:90]}`" + (f"（{c.get('runs', 1)} 次）" if c.get("runs") else ""))
    return "\n".join(parts)