"""Schema-drift detection — live catalog vs KB schema_notes (zero LLM).

The KB is the data agent's memory of the datasource shape; when the live
schema drifts (tables added/removed, columns changed) that memory silently
goes stale and generated SQL can target non-existent objects. This module
compares the live schema against the KB's ``schema_notes.yml`` table set and
reports drift so operators can re-run ``/kb init`` or flag related
lessons/examples as stale.
"""

from __future__ import annotations

from typing import Any


async def detect_drift(datasource: str, kb: Any, catalog: Any) -> dict[str, Any]:
    """Compare live schema vs KB schema_notes; return a drift report.

    Pure deterministic comparison. ``catalog.list_tables`` returns table
    dicts with ``name``/``columns``. KB table notes come from
    ``KbService.table_notes`` (keyed by table name) after ``ensure_synced``.

    Returns::

        {
          "datasource", "new_tables": [...], "gone_tables": [...],
          "column_changes": {"<table>": {"added": [...], "removed": [...]}},
        }
    """
    report: dict[str, Any] = {
        "datasource": datasource,
        "new_tables": [],
        "gone_tables": [],
        "column_changes": {},
    }
    try:
        await kb.ensure_synced(default_datasource=datasource)
    except Exception:
        pass
    live = {}
    try:
        for t in await catalog.list_tables(datasource):
            name = str(t.get("name", ""))
            live[name] = {c.get("name") for c in t.get("columns", []) if c.get("name")}
    except Exception:
        return report

    # KB 列集合直接解析 schema_notes.yml(不经过 table_notes:后者只保留
    # 有描述的列,无描述列会被丢掉 → 产生误报的列漂移)。
    kb_tables = {}
    try:
        kb_tables = _kb_column_set(kb, datasource)
    except Exception:
        pass
    if not kb_tables:
        return report

    report["new_tables"] = sorted(set(live) - set(kb_tables))
    report["gone_tables"] = sorted(set(kb_tables) - set(live))
    for table in sorted(set(live) & set(kb_tables)):
        added = sorted(live[table] - kb_tables[table])
        removed = sorted(kb_tables[table] - live[table])
        if added or removed:
            report["column_changes"][table] = {"added": added, "removed": removed}
    return report


def _kb_column_set(kb: Any, datasource: str) -> dict[str, set[str]]:
    """从 schema_notes.yml 解析 表名 → 列名集合(表/列全量,无描述过滤)。"""
    import yaml

    path = kb.kb_dir / datasource / "schema_notes.yml"
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: dict[str, set[str]] = {}
    for table in data.get("tables", []):
        name = str(table.get("name", ""))
        if not name:
            continue
        cols = {str(c.get("name", "")) for c in table.get("columns", []) if c.get("name")}
        out[name] = cols
    return out
