"""Knowledge base service — YAML source of truth, SQLite retrieval mirror.

Files (human-editable, single source of truth):
  .trove/kb/schema_notes.yml   table/column descriptions, metrics
  .trove/kb/semantics.yml      business terms → physical mappings
  .trove/kb/examples.yml       reference SQL + templates (few-shot material)

Mirror (agent reads only this):
  .trove/kb/kb.sqlite          kb_items + kb_sync tables

Sync is lazy: ensure_synced() compares per-file mtime and reloads only
changed files. Broken YAML keeps the previous mirror (queries never
block on KB health).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiosqlite
import yaml

from trove.core.logging import get_logger
from trove.core.types import SchemaInfo

logger = get_logger(__name__)

KB_DIR_NAME = "kb"

_CREATE_ITEMS = """CREATE TABLE IF NOT EXISTS kb_items (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    item_key TEXT NOT NULL,
    payload TEXT NOT NULL,
    source_file TEXT NOT NULL
)"""

_CREATE_SYNC = """CREATE TABLE IF NOT EXISTS kb_sync (
    file_path TEXT PRIMARY KEY,
    mtime REAL NOT NULL
)"""


# ── Hit models ───────────────────────────────────────────


@dataclass
class TermHit:
    """A business term matched against the user question."""

    term: str
    aliases: list[str] = field(default_factory=list)
    mapping: str = ""
    tables: list[str] = field(default_factory=list)
    definition: str = ""


@dataclass
class TableNotes:
    """Human-written annotations for one table."""

    description: str = ""
    columns: dict[str, str] = field(default_factory=dict)  # col name → description
    metrics: dict[str, str] = field(default_factory=dict)  # metric name → definition


@dataclass
class ExampleHit:
    """A reference SQL example (or template) scored against the question."""

    question: str
    sql: str
    tags: list[str] = field(default_factory=list)
    template: bool = False
    score: int = 0


# ── Pure scoring helpers ─────────────────────────────────


def _bigrams(text: str) -> set[str]:
    """Character bigrams (works for Chinese without tokenization)."""
    return {text[i : i + 2] for i in range(len(text) - 1)}


def _score_example(question: str, matched_terms: set[str], example: dict) -> int:
    """Score = 2×term hits + tag hits + bigram overlap with the question."""
    tags = [str(t) for t in example.get("tags", [])]
    ex_text = " ".join([str(example.get("question", "")), *tags])
    tag_hits = sum(1 for t in tags if t and t in question)
    term_hits = sum(1 for t in matched_terms if t and t in ex_text)
    bigram_overlap = len(_bigrams(question) & _bigrams(str(example.get("question", ""))))
    return 2 * term_hits + tag_hits + bigram_overlap


# ── YAML parsing ─────────────────────────────────────────


def _parse_file(path: Path) -> list[tuple[str, str, dict]]:
    """Parse one YAML file into (kind, item_key, payload) entries."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries: list[tuple[str, str, dict]] = []

    if path.name == "schema_notes.yml":
        for table in data.get("tables", []):
            columns = {}
            for col in table.get("columns", []):
                desc = str(col.get("description", "") or "").strip()
                if desc:
                    columns[str(col["name"])] = desc
            metrics = {}
            for metric in table.get("metrics", []):
                definition = str(metric.get("definition", "") or "").strip()
                if definition:
                    metrics[str(metric["name"])] = definition
            entries.append(("table", str(table["name"]), {
                "description": str(table.get("description", "") or "").strip(),
                "columns": columns,
                "metrics": metrics,
            }))

    elif path.name == "semantics.yml":
        for term in data.get("terms", []):
            entries.append(("term", str(term["term"]), {
                "term": str(term["term"]),
                "aliases": list(term.get("aliases") or []),
                "mapping": str(term.get("mapping", "")),
                "tables": list(term.get("tables") or []),
                "definition": str(term.get("definition", "")),
            }))

    elif path.name == "examples.yml":
        for example in data.get("examples", []):
            kind = "template" if example.get("template") else "example"
            entries.append((kind, str(example.get("question", "")), {
                "question": str(example.get("question", "")),
                "sql": str(example.get("sql", "")),
                "tags": list(example.get("tags") or []),
                "template": bool(example.get("template")),
            }))

    return entries


# ── Service ──────────────────────────────────────────────


class KbService:
    """Knowledge base: YAML source → SQLite mirror → retrieval API.

    The knowledge base is optional: when the .trove/kb directory does
    not exist, every query returns empty and the pipeline behaves
    exactly as without a KB.
    """

    def __init__(self, project_root: str | Path):
        self.kb_dir = Path(project_root) / ".trove" / KB_DIR_NAME
        self.db_path = self.kb_dir / "kb.sqlite"
        # /kb learn draft awaiting user confirmation
        self.pending_draft: dict[str, Any] | None = None

    @property
    def enabled(self) -> bool:
        return self.kb_dir.is_dir()

    # ── Sync ──────────────────────────────────────────────

    async def ensure_synced(self) -> None:
        """Lazy sync: reload only YAML files whose mtime changed."""
        if not self.enabled:
            return
        self.kb_dir.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(_CREATE_ITEMS)
            await db.execute(_CREATE_SYNC)
            await db.commit()
            for yml in sorted(self.kb_dir.glob("*.yml")):
                await self._sync_file(db, yml)

    async def _sync_file(self, db: aiosqlite.Connection, yml: Path) -> None:
        mtime = yml.stat().st_mtime
        cursor = await db.execute(
            "SELECT mtime FROM kb_sync WHERE file_path = ?", (yml.name,),
        )
        row = await cursor.fetchone()
        if row is not None and row[0] == mtime:
            return

        try:
            entries = _parse_file(yml)
        except Exception as e:
            # Never block queries on a broken KB file — keep the old mirror.
            logger.warning(
                "KB file %s failed to parse; keeping previous mirror: %s", yml, e,
            )
            return

        await db.execute("DELETE FROM kb_items WHERE source_file = ?", (yml.name,))
        for kind, item_key, payload in entries:
            await db.execute(
                "INSERT INTO kb_items (kind, item_key, payload, source_file) "
                "VALUES (?, ?, ?, ?)",
                (kind, item_key, json.dumps(payload, ensure_ascii=False), yml.name),
            )
        await db.execute(
            "INSERT OR REPLACE INTO kb_sync (file_path, mtime) VALUES (?, ?)",
            (yml.name, mtime),
        )
        await db.commit()

    async def force_sync(self) -> None:
        """Reload every YAML file unconditionally (/kb reload).

        Also purges mirror items whose source file was deleted from disk.
        """
        if not self.enabled:
            return
        self.kb_dir.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(_CREATE_ITEMS)
            await db.execute(_CREATE_SYNC)
            await db.execute("DELETE FROM kb_sync")
            yml_names = [p.name for p in self.kb_dir.glob("*.yml")]
            if yml_names:
                placeholders = ",".join("?" * len(yml_names))
                await db.execute(
                    f"DELETE FROM kb_items WHERE source_file NOT IN ({placeholders})",
                    yml_names,
                )
            else:
                await db.execute("DELETE FROM kb_items")
            await db.commit()
        await self.ensure_synced()

    # ── Retrieval ─────────────────────────────────────────

    async def _rows(self, sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(sql, params)
            return await cursor.fetchall()

    async def search_terms(self, question: str) -> list[TermHit]:
        """Terms whose term/alias is a substring of the question."""
        if not self.enabled:
            return []
        rows = await self._rows("SELECT payload FROM kb_items WHERE kind = 'term'")
        hits = []
        for row in rows:
            payload = json.loads(row["payload"])
            if payload["term"] in question or any(
                a and a in question for a in payload.get("aliases", [])
            ):
                hits.append(TermHit(**payload))
        return hits

    async def table_notes(self, table_names: list[str]) -> dict[str, TableNotes]:
        """Annotations for the given tables (empty descriptions are skipped)."""
        if not self.enabled or not table_names:
            return {}
        placeholders = ",".join("?" * len(table_names))
        rows = await self._rows(
            f"SELECT item_key, payload FROM kb_items "
            f"WHERE kind = 'table' AND item_key IN ({placeholders})",
            tuple(table_names),
        )
        return {
            row["item_key"]: TableNotes(**json.loads(row["payload"]))
            for row in rows
        }

    async def search_examples(self, question: str, limit: int = 3) -> list[ExampleHit]:
        """Top-K reference examples/templates by relevance score."""
        if not self.enabled:
            return []
        term_hits = await self.search_terms(question)
        matched_terms = {h.term for h in term_hits}
        rows = await self._rows(
            "SELECT payload FROM kb_items "
            "WHERE kind IN ('example', 'template') ORDER BY id",
        )
        scored = []
        for row in rows:
            payload = json.loads(row["payload"])
            score = _score_example(question, matched_terms, payload)
            if score > 0:
                scored.append(ExampleHit(**payload, score=score))
        scored.sort(key=lambda h: h.score, reverse=True)
        return scored[:limit]

    async def list_items(self) -> dict[str, int]:
        """Item counts per kind (/kb list)."""
        if not self.enabled:
            return {}
        rows = await self._rows(
            "SELECT kind, COUNT(*) AS n FROM kb_items GROUP BY kind",
        )
        return {row["kind"]: row["n"] for row in rows}

    # ── Evolution (human-confirmed writes) ────────────────

    async def append_example(self, entry: dict[str, Any]) -> None:
        """Append a reference-SQL/template entry to examples.yml and re-sync."""
        await self._append_entry("examples.yml", "examples", entry)

    async def append_term(self, entry: dict[str, Any]) -> None:
        """Append a business term to semantics.yml and re-sync."""
        await self._append_entry("semantics.yml", "terms", entry)

    async def _append_entry(self, filename: str, section: str, entry: dict) -> None:
        self.kb_dir.mkdir(parents=True, exist_ok=True)
        path = self.kb_dir / filename
        data = {}
        if path.exists():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        items = list(data.get(section, []))
        items.append(entry)
        data[section] = items
        path.write_text(
            yaml.safe_dump(
                data, default_flow_style=False, allow_unicode=True, sort_keys=False,
            ),
            encoding="utf-8",
        )
        await self.force_sync()

    # ── Initialization ────────────────────────────────────

    def init_schema_notes(self, schema: SchemaInfo, overwrite: bool = False) -> bool:
        """Generate a schema_notes.yml skeleton from the datasource schema.

        Returns:
            True if created, False if the file already exists (refusing to overwrite).
        """
        self.kb_dir.mkdir(parents=True, exist_ok=True)
        path = self.kb_dir / "schema_notes.yml"
        if path.exists() and not overwrite:
            logger.info("schema_notes.yml already exists; refusing to overwrite")
            return False

        tables = []
        for table in schema.tables:
            tables.append({
                "name": table.name,
                "description": "",
                "columns": [
                    {"name": c.name, "description": "", "enums": []}
                    for c in table.columns
                ],
                "metrics": [],
            })
        path.write_text(
            yaml.safe_dump(
                {"tables": tables},
                default_flow_style=False, allow_unicode=True, sort_keys=False,
            ),
            encoding="utf-8",
        )
        return True
