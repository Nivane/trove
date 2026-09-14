"""Schema / semantic-model drift check — cron/CI runnable, zero LLM.

Compares every registered datasource's **live schema** against two
declared shapes:

  - KB drift (``schema_notes.yml``): new/gone tables, added/removed
    columns (``detect_drift``).
  - Semantic drift (``semantics.yml`` via ``SemanticLayerProvider``):
    declared datasets / fields / keys / relationship endpoints that no
    longer match the live schema.

This is the alerting front door for the runbook: the serve periodic
sweep only *logs* drift, this script turns it into an exit code.

Exit codes (for cron / CI):
  0  clean — no drift
  1  drift found (alert)
  2  error — no datasources.yml, connection failure, invalid args

Usage:
    uv run python scripts/check_drift.py [--datasource NAME_OR_URL] [--json]
        [--kb-dir DIR] [--verbose]

Datasources come from ``.trove/datasources.yml`` (what serve registered);
``--datasource`` selects one by registered name or a ``scheme://...`` URL.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from trove.services.datasource.config_store import ConfigStore
from trove.services.datasource.registry import ConnectorRegistry
from trove.services.datasource.urls import parse_datasource_url
from trove.services.kb.service import KbService, resolve_kb_root
from trove.services.memory.schema_drift import detect_drift


class _DictCatalog:
    """Adapter-shaped catalog over an already-fetched live schema."""

    def __init__(self, data: dict[str, set[str]]) -> None:
        self._data = data

    async def column_sets(self, datasource: str) -> dict[str, set[str]]:
        return self._data


async def _live_catalog(adapter) -> dict[str, set[str]]:
    schema = await adapter.get_schema()
    return {t.name: {c.name for c in t.columns} for t in schema.tables}


async def _check_datasource(name: str, cfg, kb, kb_dir: str | None,
                            verbose: bool) -> dict:
    registry = ConnectorRegistry()
    try:
        if getattr(cfg, "type", "") == "demo":
            from trove.services.datasource.demo_setup import setup_demo_datasource
            await setup_demo_datasource(registry, set_default=False)
        else:
            await registry.register(cfg)
        adapter = await registry.get(name)
        catalog = await _live_catalog(adapter)
        dialect = adapter.dialect() or "sqlite"

        kb_report = await detect_drift(name, kb, _DictCatalog(catalog))

        semantic = {"stale": False, "detail": "no semantic layer"}
        semantics_path = kb.semantics_path(name)
        if semantics_path.exists():
            from trove.services.semantic_layer.provider import SemanticLayerProvider
            provider = SemanticLayerProvider(
                directory=Path.cwd() / ".trove" / "semantic" / name,
                datasource=name,
                dialect=dialect,
                kb_semantics_path=semantics_path,
                catalog={t.lower(): {c.lower() for c in cols}
                         for t, cols in catalog.items()},
            )
            report = provider.drift()
            semantic = report
        return {"kb": kb_report, "semantic": semantic}
    finally:
        await registry.close_all()


async def _run(args) -> tuple[dict, int]:
    store = ConfigStore()
    configs = store.load_configs()
    if args.datasource:
        if "://" in args.datasource:
            cfg = parse_datasource_url(args.datasource)
            configs = [cfg]
        else:
            configs = [c for c in configs if c.name == args.datasource]
        if not configs:
            return {"error": f"datasource not found: {args.datasource}"}, 2

    if not configs:
        return {"error": ".trove/datasources.yml 无注册数据源"}, 2

    kb_root = resolve_kb_root(args.kb_dir, configs[0].name)
    kb = KbService(Path.cwd(), kb_dir=kb_root)

    reports = {}
    errors = []
    for cfg in configs:
        try:
            reports[cfg.name] = await _check_datasource(
                cfg.name, cfg, kb, args.kb_dir, args.verbose)
        except Exception as e:
            errors.append(f"{cfg.name}: {e}")

    dirty = {}
    for name, rep in reports.items():
        kb_dirty = bool(rep["kb"]["new_tables"] or rep["kb"]["gone_tables"]
                        or rep["kb"]["column_changes"])
        sem_stale = bool(rep["semantic"].get("stale"))
        if kb_dirty or sem_stale:
            dirty[name] = rep

    if errors:
        return {"datasources": reports, "errors": errors}, 2
    return {"datasources": reports}, 1 if dirty else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--datasource", default=None,
                        help="仅检查指定数据源(已注册名或 scheme:// URL)")
    parser.add_argument("--kb-dir", default=None,
                        help="KB 根目录;默认 <cwd>/.trove/kb")
    parser.add_argument("--json", action="store_true",
                        help="输出机器可读 JSON(报警用)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    result, code = asyncio.run(_run(args))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, default=str))
    else:
        _render_human(result)
    return code


def _render_human(result: dict) -> None:
    errors = result.get("errors")
    if errors:
        print("== ERRORS ==", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return
    for name, rep in sorted(result.get("datasources", {}).items()):
        kb = rep["kb"]
        print(f"== {name} ==")
        kb_dirty = bool(kb["new_tables"] or kb["gone_tables"] or kb["column_changes"])
        print(f"KB drift: {'OK' if not kb_dirty else 'DRIFT'}")
        for t in kb["new_tables"]:
            print(f"  + new table: {t}")
        for t in kb["gone_tables"]:
            print(f"  - gone table: {t}")
        for table, ch in kb["column_changes"].items():
            for c in ch["added"]:
                print(f"  + {table}.{c} added")
            for c in ch["removed"]:
                print(f"  - {table}.{c} removed")
        sem = rep["semantic"]
        if isinstance(sem, dict) and "stale" in sem:
            status = "STALE" if sem["stale"] else "OK"
            print(f"Semantic drift: {status}")
            for t in sem.get("gone_tables", []):
                print(f"  - gone dataset: {t}")
            for ds, fields in sem.get("missing_fields", {}).items():
                print(f"  - {ds} missing fields: {fields}")
            for ds, keys in sem.get("missing_keys", {}).items():
                print(f"  - {ds} missing keys: {keys}")
            for rb in sem.get("relationship_breaks", []):
                print(f"  - relationship {rb.get('name')}: {rb.get('detail')}")
        else:
            print(f"Semantic drift: {sem.get('detail', 'n/a')}")


if __name__ == "__main__":
    sys.exit(main())
