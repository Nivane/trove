"""Knowledge base service — per-datasource YAML source, SQLite retrieval mirror.

Files (human-editable, single source of truth):
  .trove/kb/<datasource>/schema_notes.yml   table/column descriptions, metrics
  .trove/kb/<datasource>/semantics.yml      business terms → physical mappings
  .trove/kb/<datasource>/examples.yml       reference SQL + templates (few-shot)

Mirror (agent reads only this):
  .trove/kb/kb.sqlite          kb_items + kb_sync tables

Sync is lazy: ensure_synced() compares per-file mtime and reloads only
changed files. Broken YAML keeps the previous mirror (queries never
block on KB health). Legacy flat YAML files (kb root, pre-datasource
layout) are auto-migrated into a datasource subdirectory.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiosqlite
import yaml

from trove.core.logging import get_logger
from trove.core.types import SchemaInfo

logger = get_logger(__name__)

KB_DIR_NAME = "kb"
LEGACY_DIR_NAME = "legacy"

_CREATE_ITEMS = """CREATE TABLE IF NOT EXISTS kb_items (
    id INTEGER PRIMARY KEY,
    datasource TEXT NOT NULL,
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
    enums: dict[str, str] = field(default_factory=dict)  # col name → enum value description


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


def _word_tokens(text: str) -> set[str]:
    """Lowercased ASCII word tokens (empty for pure-Chinese text)."""
    return set(re.findall(r"[a-zA-Z0-9_]+", text.lower()))


def _mentions_any(text: str, names: list[str]) -> bool:
    """Deterministic lexical check: does text mention any of the names."""
    return any(n and n in text for n in names)


def _score_example(
    question: str,
    matched_terms: set[str],
    example: dict,
    tables: list[str] | None = None,
) -> int:
    """Score = 2×term hits + tag hits + overlap with the question + table anchor.

    Overlap is word-level for English questions (char-bigrams are
    meaningless there) and char-bigram for Chinese; the max of the two
    covers mixed-language questions. A matched-table mention adds a
    strong +3 anchor per table (evidence-graph scoring).
    """
    tags = [str(t) for t in example.get("tags", [])]
    ex_text = " ".join([str(example.get("question", "")), *tags])
    tag_hits = sum(1 for t in tags if t and t in question)
    term_hits = sum(1 for t in matched_terms if t and t in ex_text)

    example_question = str(example.get("question", ""))
    q_words = _word_tokens(question)
    ex_words = _word_tokens(example_question)
    if q_words and ex_words:
        # English (or mixed) questions: word-level overlap only —
        # char bigrams are noise on space-separated text.
        overlap = len(q_words & ex_words)
    else:
        # Pure-Chinese questions: character bigram overlap.
        overlap = len(_bigrams(question) & _bigrams(example_question))

    table_anchor = 0
    if tables:
        full_text = " ".join([example_question, str(example.get("sql", "")), *tags])
        table_anchor = 3 * sum(1 for t in tables if t and t in full_text)

    return 2 * term_hits + tag_hits + overlap + table_anchor


# ── YAML parsing ─────────────────────────────────────────


def _parse_file(path: Path) -> list[tuple[str, str, dict]]:
    """Parse one YAML file into (kind, item_key, payload) entries."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries: list[tuple[str, str, dict]] = []

    if path.name == "schema_notes.yml":
        for table in data.get("tables", []):
            columns = {}
            enums = {}
            for col in table.get("columns", []):
                desc = str(col.get("description", "") or "").strip()
                if desc:
                    columns[str(col["name"])] = desc
                enum_text = "; ".join(
                    str(e).strip() for e in (col.get("enums") or [])
                    if str(e).strip()
                )
                if enum_text:
                    enums[str(col["name"])] = enum_text
            metrics = {}
            for metric in table.get("metrics", []):
                definition = str(metric.get("definition", "") or "").strip()
                if definition:
                    metrics[str(metric["name"])] = definition
            entries.append(("table", str(table["name"]), {
                "description": str(table.get("description", "") or "").strip(),
                "columns": columns,
                "metrics": metrics,
                "enums": enums,
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

    elif path.name == "rules.yml":
        for rule in data.get("rules", []):
            text = str(rule.get("rule", "")).strip()
            if text:
                entries.append(("rule", text, {"rule": text}))

    elif path.name == "lessons.yml":
        for lesson in data.get("lessons", []):
            entries.append(("lesson", str(lesson.get("pattern", "")), {
                "pattern": str(lesson.get("pattern", "")),
                "note": str(lesson.get("note", "")),
                "sql_snippet": str(lesson.get("sql_snippet", "")),
                "confirmed": bool(lesson.get("confirmed", False)),
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
    """Knowledge base: per-datasource YAML source → SQLite mirror → retrieval.

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

    async def ensure_synced(self, default_datasource: str | None = None) -> None:
        """Lazy sync: reload only YAML files whose mtime changed.

        Args:
            default_datasource: Target subdirectory for migrating legacy
                flat YAML files left at the kb root (pre-datasource layout).
        """
        if not self.enabled:
            return
        self.kb_dir.mkdir(parents=True, exist_ok=True)
        self._migrate_legacy_files(default_datasource)

        async with aiosqlite.connect(self.db_path) as db:
            await self._ensure_mirror_schema(db)
            for ds_dir in sorted(self._datasource_dirs()):
                for yml in sorted(ds_dir.glob("*.yml")):
                    await self._sync_file(db, yml, ds_dir.name)

    async def _sync_file(
        self, db: aiosqlite.Connection, yml: Path, datasource: str,
    ) -> None:
        rel_path = f"{datasource}/{yml.name}"
        mtime = yml.stat().st_mtime
        cursor = await db.execute(
            "SELECT mtime FROM kb_sync WHERE file_path = ?", (rel_path,),
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

        await db.execute(
            "DELETE FROM kb_items WHERE datasource = ? AND source_file = ?",
            (datasource, yml.name),
        )
        for kind, item_key, payload in entries:
            await db.execute(
                "INSERT INTO kb_items (datasource, kind, item_key, payload, source_file) "
                "VALUES (?, ?, ?, ?, ?)",
                (datasource, kind, item_key, json.dumps(payload, ensure_ascii=False), yml.name),
            )
        await db.execute(
            "INSERT OR REPLACE INTO kb_sync (file_path, mtime) VALUES (?, ?)",
            (rel_path, mtime),
        )
        await db.commit()

    async def force_sync(self, default_datasource: str | None = None) -> None:
        """Reload every YAML file unconditionally (/kb reload).

        Also purges mirror items whose source file was deleted from disk.
        """
        if not self.enabled:
            return
        self.kb_dir.mkdir(parents=True, exist_ok=True)
        self._migrate_legacy_files(default_datasource)

        current_files = {
            f"{ds_dir.name}/{yml.name}"
            for ds_dir in self._datasource_dirs()
            for yml in ds_dir.glob("*.yml")
        }
        async with aiosqlite.connect(self.db_path) as db:
            await self._ensure_mirror_schema(db)
            await db.execute("DELETE FROM kb_sync")
            if current_files:
                rows = await (await db.execute(
                    "SELECT file_path FROM kb_sync"
                )).fetchall()  # placeholder to keep structure clear
                placeholders = ",".join("?" * len(current_files))
                await db.execute(
                    f"DELETE FROM kb_items WHERE source_file NOT IN ({placeholders})",
                    [f.split("/", 1)[1] for f in current_files],
                )
            else:
                await db.execute("DELETE FROM kb_items")
            await db.commit()
        await self.ensure_synced(default_datasource)

    def _datasource_dirs(self) -> list[Path]:
        """Subdirectories of kb_dir (each named after a datasource)."""
        return [p for p in sorted(self.kb_dir.iterdir()) if p.is_dir()]

    def _migrate_legacy_files(self, default_datasource: str | None) -> None:
        """Move pre-datasource flat YAML files into a datasource subdirectory."""
        target = default_datasource or LEGACY_DIR_NAME
        for yml in sorted(self.kb_dir.glob("*.yml")):
            dest_dir = self.kb_dir / target
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / yml.name
            if dest.exists():
                logger.warning(
                    "KB migration: %s exists in %s/; leaving legacy file in place",
                    yml.name, target,
                )
                continue
            shutil.move(str(yml), str(dest))
            logger.info("KB migrated %s → %s/", yml.name, target)

    async def _ensure_mirror_schema(self, db: aiosqlite.Connection) -> None:
        """Create mirror tables; rebuild if the schema predates the datasource column."""
        cursor = await db.execute("PRAGMA table_info(kb_items)")
        columns = {row[1] for row in await cursor.fetchall()}
        if columns and "datasource" not in columns:
            logger.info("Rebuilding KB mirror (missing datasource column)")
            await db.execute("DROP TABLE kb_items")
            await db.execute("DROP TABLE IF EXISTS kb_sync")
            columns = set()
        if not columns:
            await db.execute(_CREATE_ITEMS)
            await db.execute(_CREATE_SYNC)
            await db.commit()

    # ── Retrieval ─────────────────────────────────────────

    async def _rows(self, sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(sql, params)
            return await cursor.fetchall()

    async def search_terms(
        self,
        question: str,
        datasource: str,
        tables: list[str] | None = None,
        all_tables: list[str] | None = None,
    ) -> list[TermHit]:
        """Terms whose term/alias is a substring of the question.

        With `tables` given (schema-linked tables), terms bound to other
        tables are dropped; terms bound to no table at all are kept
        (table-agnostic business semantics).
        """
        if not self.enabled:
            return []
        rows = await self._rows(
            "SELECT payload FROM kb_items WHERE kind = 'term' AND datasource = ?",
            (datasource,),
        )
        hits = []
        for row in rows:
            payload = json.loads(row["payload"])
            if not (
                payload["term"] in question
                or any(a and a in question for a in payload.get("aliases", []))
            ):
                continue
            if tables is not None:
                bound = payload.get("tables", []) or []
                if bound and not any(t in tables for t in bound):
                    continue  # 术语绑定到未匹配的表 → 与当前问题无关
                payload_text = " ".join(
                    [payload.get("term", ""), payload.get("mapping", ""),
                     payload.get("definition", ""), " ".join(bound)]
                )
                if all_tables and _mentions_any(payload_text, all_tables) and not _mentions_any(payload_text, tables):
                    continue  # 文本提到其他表 → 过滤
            hits.append(TermHit(**payload))
        return hits

    async def table_notes(
        self, table_names: list[str], datasource: str,
    ) -> dict[str, TableNotes]:
        """Annotations for the given tables (empty descriptions are skipped)."""
        if not self.enabled or not table_names:
            return {}
        placeholders = ",".join("?" * len(table_names))
        rows = await self._rows(
            f"SELECT item_key, payload FROM kb_items "
            f"WHERE kind = 'table' AND datasource = ? "
            f"AND item_key IN ({placeholders})",
            (datasource, *table_names),
        )
        return {
            row["item_key"]: TableNotes(**json.loads(row["payload"]))
            for row in rows
        }

    async def search_examples(
        self,
        question: str,
        datasource: str,
        limit: int = 3,
        tables: list[str] | None = None,
        all_tables: list[str] | None = None,
    ) -> list[ExampleHit]:
        """Top-K reference examples/templates by relevance score.

        With `tables` given, examples that mention OTHER tables are
        dropped (deterministic evidence filtering); examples mentioning
        the matched tables get a score anchor.
        """
        if not self.enabled:
            return []
        term_hits = await self.search_terms(question, datasource)
        matched_terms = {h.term for h in term_hits}
        rows = await self._rows(
            "SELECT payload FROM kb_items "
            "WHERE kind IN ('example', 'template') AND datasource = ? ORDER BY id",
            (datasource,),
        )
        scored = []
        for row in rows:
            payload = json.loads(row["payload"])
            full_text = " ".join([
                str(payload.get("question", "")),
                str(payload.get("sql", "")),
                *[str(t) for t in payload.get("tags", [])],
            ])
            if tables is not None and all_tables:
                mentioned = [t for t in all_tables if t and t in full_text]
                if mentioned and not any(t in tables for t in mentioned):
                    continue  # 示例绑定到未匹配的表 → 与当前问题无关
            score = _score_example(question, matched_terms, payload, tables=tables)
            if score > 0:
                scored.append(ExampleHit(**payload, score=score))
        scored.sort(key=lambda h: h.score, reverse=True)
        return scored[:limit]

    async def list_rules(self, datasource: str) -> list[str]:
        """Business rules of one datasource (injected into generation)."""
        if not self.enabled:
            return []
        rows = await self._rows(
            "SELECT item_key FROM kb_items WHERE kind = 'rule' AND datasource = ? ORDER BY id",
            (datasource,),
        )
        return [row["item_key"] for row in rows]

    async def list_lessons(self, datasource: str, confirmed_only: bool = True) -> list[dict]:
        """Known pitfalls (Hint Bank); pending ones excluded by default."""
        if not self.enabled:
            return []
        rows = await self._rows(
            "SELECT payload FROM kb_items WHERE kind = 'lesson' AND datasource = ? ORDER BY id",
            (datasource,),
        )
        lessons = [json.loads(row["payload"]) for row in rows]
        if confirmed_only:
            lessons = [l for l in lessons if l.get("confirmed")]
        return lessons

    async def append_lesson(self, entry: dict, datasource: str) -> None:
        """Record a lesson candidate (pending until confirmed)."""
        entry.setdefault("confirmed", False)
        await self._append_entry("lessons.yml", "lessons", entry, datasource)

    async def confirm_pending_lessons(self, datasource: str) -> int:
        """Mark all pending lessons as confirmed (rewrites the YAML)."""
        path = self.kb_dir / datasource / "lessons.yml"
        if not path.exists():
            return 0
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        confirmed = 0
        for lesson in data.get("lessons", []):
            if not lesson.get("confirmed"):
                lesson["confirmed"] = True
                confirmed += 1
        path.write_text(
            yaml.safe_dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        await self.force_sync(datasource)
        return confirmed

    async def list_term_names(self, datasource: str) -> list[str]:
        """Term names of one datasource (knowledge intent answers)."""
        if not self.enabled:
            return []
        rows = await self._rows(
            "SELECT item_key FROM kb_items WHERE kind = 'term' AND datasource = ? "
            "ORDER BY item_key",
            (datasource,),
        )
        return [row["item_key"] for row in rows]

    async def list_example_questions(self, datasource: str) -> list[str]:
        """Example/template questions of one datasource."""
        if not self.enabled:
            return []
        rows = await self._rows(
            "SELECT item_key FROM kb_items "
            "WHERE kind IN ('example', 'template') AND datasource = ? "
            "ORDER BY id",
            (datasource,),
        )
        return [row["item_key"] for row in rows]

    async def list_items(self) -> dict[str, dict[str, int]]:
        """Item counts per kind, grouped by datasource (/kb list)."""
        if not self.enabled:
            return {}
        rows = await self._rows(
            "SELECT datasource, kind, COUNT(*) AS n FROM kb_items "
            "GROUP BY datasource, kind",
        )
        grouped: dict[str, dict[str, int]] = {}
        for row in rows:
            grouped.setdefault(row["datasource"], {})[row["kind"]] = row["n"]
        return grouped

    # ── Evolution (human-confirmed writes) ────────────────

    async def append_example(self, entry: dict[str, Any], datasource: str) -> None:
        """Append a reference-SQL/template entry to the datasource's examples.yml."""
        await self._append_entry("examples.yml", "examples", entry, datasource)

    async def append_term(self, entry: dict[str, Any], datasource: str) -> None:
        """Append a business term to the datasource's semantics.yml."""
        await self._append_entry("semantics.yml", "terms", entry, datasource)

    async def _append_entry(
        self, filename: str, section: str, entry: dict, datasource: str,
    ) -> None:
        ds_dir = self.kb_dir / datasource
        ds_dir.mkdir(parents=True, exist_ok=True)
        path = ds_dir / filename
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

    def init_exists(self, datasource: str) -> list[str]:
        """Which of the three KB files already exist for a datasource."""
        ds_dir = self.kb_dir / datasource
        return [
            name for name in ("schema_notes.yml", "semantics.yml", "examples.yml")
            if (ds_dir / name).exists()
        ]

    def _init_file(
        self, filename: str, section: str, items: list[dict], datasource: str,
        overwrite: bool = False,
    ) -> bool:
        """Write a KB file, refusing to overwrite an existing one."""
        ds_dir = self.kb_dir / datasource
        ds_dir.mkdir(parents=True, exist_ok=True)
        path = ds_dir / filename
        if path.exists() and not overwrite:
            logger.info("%s/%s already exists; refusing to overwrite", datasource, filename)
            return False
        path.write_text(
            yaml.safe_dump(
                {section: items},
                default_flow_style=False, allow_unicode=True, sort_keys=False,
            ),
            encoding="utf-8",
        )
        return True

    def init_schema_notes(
        self, schema: SchemaInfo, datasource: str, overwrite: bool = False,
    ) -> bool:
        """Generate a schema_notes.yml skeleton for the given datasource.

        Returns:
            True if created, False if the file already exists (refusing to overwrite).
        """
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
        return self._init_file("schema_notes.yml", "tables", tables, datasource, overwrite)

    def init_notes(
        self, tables: list[dict], datasource: str, overwrite: bool = False,
    ) -> bool:
        """Write an annotated schema_notes.yml (LLM-assisted /kb init)."""
        return self._init_file("schema_notes.yml", "tables", tables, datasource, overwrite)

    def init_terms(
        self, terms: list[dict], datasource: str, overwrite: bool = False,
    ) -> bool:
        """Write a semantics.yml (LLM-assisted /kb init)."""
        return self._init_file("semantics.yml", "terms", terms, datasource, overwrite)

    def init_examples(
        self, examples: list[dict], datasource: str, overwrite: bool = False,
    ) -> bool:
        """Write an examples.yml (LLM-assisted /kb init)."""
        return self._init_file("examples.yml", "examples", examples, datasource, overwrite)
