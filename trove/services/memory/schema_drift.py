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

    Pure deterministic comparison. ``catalog`` is queried through
    ``column_sets`` (preferred; full column names in one fetch — e.g.
    ``CatalogService.column_sets``) or a ``list_tables`` that returns
    table dicts with a ``columns`` list. KB table notes come from
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
        live = await _live_column_sets(catalog, datasource)
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
        live_cols = live[table]
        if live_cols is None:
            continue  # 列集合未知(count-only catalog)→ 列漂移不可判,别误报
        added = sorted(live_cols - kb_tables[table])
        removed = sorted(kb_tables[table] - live_cols)
        if added or removed:
            report["column_changes"][table] = {"added": added, "removed": removed}
    return report


async def _live_column_sets(catalog: Any, datasource: str) -> dict[str, set[str] | None]:
    """Live schema as ``{table: {column}}``, lowercased for comparison.

    Prefers ``catalog.column_sets`` (full column names in one fetch);
    falls back to ``list_tables`` entries with a real ``columns`` list.
    A count-only ``columns`` (e.g. ``CatalogService.list_tables`` returns
    ``len(t.columns)``) cannot report column drift — the table is kept in
    the key set (so it is not misreported as gone) but marked ``None`` so
    its column changes are skipped.
    """
    column_sets = getattr(catalog, "column_sets", None)
    if column_sets is not None:
        raw = await column_sets(datasource)
        return {
            str(t).lower(): {str(c).lower() for c in cols}
            for t, cols in raw.items()
        }
    live: dict[str, set[str] | None] = {}
    for t in await catalog.list_tables(datasource):
        name = str(t.get("name", "")).lower()
        if not name:
            continue
        cols = t.get("columns", [])
        if not isinstance(cols, list):
            live[name] = None
            continue
        live[name] = {str(c.get("name", "")).lower() for c in cols if c.get("name")}
    return live


def _kb_column_set(kb: Any, datasource: str) -> dict[str, set[str]]:
    """从 schema_notes.yml 解析 表名 → 列名集合(表/列全量,无描述过滤)。

    表/列名统一小写,与 ``_live_column_sets`` 的归一化口径一致,避免
    大小写差异造成假漂移(物理库列大小写保留、schema_notes 手写大小写
    不一的场景)。
    """
    import yaml

    path = kb.kb_dir / datasource / "schema_notes.yml"
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: dict[str, set[str]] = {}
    for table in data.get("tables", []):
        name = str(table.get("name", "")).lower()
        if not name:
            continue
        cols = {str(c.get("name", "")).lower() for c in table.get("columns", []) if c.get("name")}
        out[name] = cols
    return out
