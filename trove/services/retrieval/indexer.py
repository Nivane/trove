"""Indexer: build retrievable documents from KB YAML + physical schema.

Wires two corpora into a ``HybridStore``:

- KB items (example/template/lesson/term/table/rule) → ``kb`` kind docs, built
  from the parsed mirror. Re-indexing is idempotent (stable ``item_key``).
- Physical schema metadata (from ``CatalogService``) → ``schema_doc`` kind docs.
  This is the "catalog probing as documents" piece: table/column/type/PK/FK
  become retrievable, so the agent can reason about real schema without a
  live probing tool call. Incremental: a hash of the schema snapshot is cached;
  re-index only fires when the schema actually changed (or ``rebuild``/force).

Both corpora live behind the semantic-first boundary — the store returns docs,
the query_sketch/compiler still validate everything against the declared model.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from trove.core.logging import get_logger
from trove.services.datasource.catalog import CatalogService
from trove.services.kb.backends.fts import fts_item_text
from trove.services.retrieval.store import HybridStore, RetrievalDoc

logger = get_logger(__name__)

_SCHEMA_SOURCE_PREFIX = "schema:"


def _schema_snapshot(schema: Any) -> dict:
    """Stable JSON snapshot of physical schema for change detection."""
    out: dict = {}
    for t in schema.tables:
        cols = {
            c.name: {
                "type": c.type,
                "pk": bool(c.primary_key),
                "fk": c.foreign_key if hasattr(c, "foreign_key") else None,
            }
            for c in t.columns
        }
        out[t.name] = {"columns": cols, "rows": getattr(t, "row_count_estimate", None)}
    return out


def _schema_doc_text(name: str, table: dict) -> str:
    parts = [f"Table {name}"]
    rows = table.get("rows")
    if rows is not None:
        parts.append(f"(approx {rows} rows)")
    col_bits = []
    for col, meta in table.get("columns", {}).items():
        bits = [col, str(meta.get("type") or "")]
        if meta.get("pk"):
            bits.append("PRIMARY KEY")
        if meta.get("fk"):
            bits.append(f"FK->{meta['fk']}")
        col_bits.append(" ".join(b for b in bits if b))
    parts.append("Columns: " + "; ".join(col_bits))
    return "\n".join(parts)


class Indexer:
    def __init__(
        self, store: HybridStore, kb: Any, connectors: Any, home: str | Path,
    ) -> None:
        self._store = store
        self._kb = kb
        self._connectors = connectors
        self._home = Path(home)
        self._cache_dir = self._home / "retrieval"
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    async def index_kb(self, datasource: str, rebuild: bool = False) -> int:
        if rebuild:
            await self._store.delete_source(datasource, "kb")
        items = await self._kb.iter_items(datasource)
        docs: list[RetrievalDoc] = []
        for it in items:
            text = self._kb_text(it)
            if not text:
                continue
            docs.append(RetrievalDoc(
                content=text, datasource=datasource, kind="kb",
                source_file=it["source_file"], item_key=it["item_key"],
            ))
        if docs:
            await self._store.index_many(docs)
        return len(docs)

    def _kb_text(self, item: dict) -> str:
        kind = item["kind"]
        payload = item["payload"]
        if kind in ("example", "template", "lesson", "metric", "entity"):
            return fts_item_text(kind, payload)
        # term/table/rule: compact readable text for dense + FTS recall
        if kind == "term":
            bits = [payload.get("term"), *payload.get("aliases", [])]
            if payload.get("definition"):
                bits.append(payload["definition"])
            if payload.get("mapping"):
                bits.append(f"maps to {payload['mapping']}")
            return " ".join(str(b) for b in bits if b)
        if kind == "table":
            return fts_item_text("table", payload) or json.dumps(payload, ensure_ascii=False)
        if kind == "rule":
            return str(payload.get("rule") or payload.get("text") or "")
        return ""

    async def index_schema(self, datasource: str, rebuild: bool = False) -> int:
        try:
            schema = await self._connectors.get_schema(datasource)
        except Exception as e:
            logger.warning("schema snapshot failed for %s: %s", datasource, e)
            return 0
        snapshot = _schema_snapshot(schema)
        if not snapshot:
            return 0
        digest = hashlib.sha256(
            json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        cache = self._cache_dir / f"{datasource}_schema_hash.txt"
        prev = cache.read_text().strip() if cache.exists() else ""
        source = _SCHEMA_SOURCE_PREFIX + datasource
        if not rebuild and prev == digest:
            return 0  # 未变化 → 不重嵌(增量)
        # 重新索引该数据源的全部 schema doc(删除旧的再写新的)
        await self._store.delete_source(datasource, source)
        docs = [
            RetrievalDoc(
                content=_schema_doc_text(name, table), datasource=datasource,
                kind="schema_doc", source_file=source, item_key=f"{source}:{name}",
            )
            for name, table in snapshot.items()
        ]
        if docs:
            await self._store.index_many(docs)
        cache.write_text(digest)
        return len(docs)

    async def sync(
        self, datasource: str, rebuild: bool = False, force_schema: bool = False,
    ) -> dict[str, int]:
        await self._kb.ensure_synced(default_datasource=datasource)
        kb_n = await self.index_kb(datasource, rebuild=rebuild)
        schema_n = await self.index_schema(datasource, rebuild=rebuild or force_schema)
        return {"kb": kb_n, "schema": schema_n}
