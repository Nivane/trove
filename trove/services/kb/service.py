"""Knowledge base service — per-datasource YAML source, SQLite retrieval mirror.

Files (human-editable, single source of truth):
  .trove/kb/<datasource>/schema_notes.yml   table/column descriptions, metrics
  .trove/kb/<datasource>/semantics.yml      OSSIE semantic_model (datasets + metrics)
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
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiosqlite
import yaml

from trove.core.logging import get_logger

_CJK_RE = re.compile(r"[一-鿿]")
from trove.core.types import SchemaInfo
from trove.services.kb.compose import compose_candidates
from trove.services.kb.ossie_format import (
    append_term_to_document,
    ossie_to_term_payloads,
    qualify_mapping,
    terms_to_ossie_document,
)

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
    stats: dict[str, dict[str, Any]] = field(default_factory=dict)  # col → profiling stats
    row_count: int | None = None  # 精确行数(profiling 写入,区别于 catalog 估算)


@dataclass
class ExampleHit:
    """A reference SQL example (or template) scored against the question."""

    question: str
    sql: str
    tags: list[str] = field(default_factory=list)
    template: bool = False
    score: int = 0
    aggregate: bool = False
    date_range: bool = False


# ── Pure scoring helpers ─────────────────────────────────


def _bigrams(text: str) -> set[str]:
    """Character bigrams (works for Chinese without tokenization)."""
    return {text[i : i + 2] for i in range(len(text) - 1)}


_STOP_WORDS = {
    "the", "a", "an", "of", "for", "and", "or", "in", "with", "at",
    "to", "on", "by", "is", "are", "per", "each",
}


def _word_tokens(text: str) -> set[str]:
    """Lowercased ASCII word tokens with naive plural stripping.

    "clients" vs "client"、"withdrawals" vs "withdrawal" 复数形式导致
    overlap 丢失("male customers" 题漏 gender 列就是这个词法死区)。
    仅去尾 s(长度>3 保护 "is/us/its");交集双方同归一,一致性不受影响。
    """
    out = set()
    for w in re.findall(r"[a-zA-Z0-9_]+", text.lower()):
        out.add(w[:-1] if len(w) > 3 and w.endswith("s") else w)
    return out


def _term_word_overlap(term: str, question: str) -> float:
    """术语词重叠率:术语有效词(去停用词)在问题中出现的比例。

    子串匹配对 paraphrase 完全失效("average approved amount" vs
    "average loan amount" 措辞不同但语义等价)——词重叠 ≥0.5 且
    ≥2 词时视为同一语义的检索命中。
    """
    tw = _word_tokens(term) - _STOP_WORDS
    if len(tw) < 2:
        return 0.0
    qw = _word_tokens(question)
    return len(tw & qw) / len(tw)


def _mentions_any(text: str, names: list[str]) -> bool:
    """Deterministic lexical check: does text mention any of the names."""
    return any(n and n in text for n in names)


def resolve_kb_root(kb_dir: str | None, db_id: str) -> Path | None:
    """Resolve a --kb-dir argument to a KB root in per-datasource layout.

    - None → 默认布局(<cwd>/.trove/kb),由 KbService 自己解析;
    - 含 <db_id>/ 子目录 → 直接当作 KB 根;
    - 扁平 YAML 目录(如手工整理的 KB 文件夹)→ 软链到临时根下的
      <db_id>/,让 KbService 按 datasource 布局读取;
    - 其他情况 → ValueError(调用方转 argparse 错误)。
    """
    if not kb_dir:
        return None
    root = Path(kb_dir)
    if not root.is_dir():
        raise ValueError(f"--kb-dir 不存在: {root}")
    if (root / db_id).is_dir():
        return root
    if any(root.glob("*.yml")):
        staged = Path(tempfile.mkdtemp(prefix="trove-kb-"))
        (staged / db_id).symlink_to(root, target_is_directory=True)
        return staged
    raise ValueError(f"--kb-dir 需包含 {db_id}/ 子目录或直接包含 YAML 文件: {root}")


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

    Aggregate templates (SELECT MAX/MIN/AVG/SUM(col) FROM t) get a 0.6
    discount: they carry one column of low-density structure and duplicate
    the SUM/AVG semantics already injected via terms — undiscounted they
    crowd JOIN skeletons out of top-k (eval_retrieval: B@5 coverage flat,
    sim@5 66%→59%).

    Date-range templates (probe 通道的年份/区间/等值/比较派生模板) get the
    same 0.6 discount: their "How many {table} records ..." wording shares
    how/many/{table} with every count question, so undiscounted they crowd
    top-k on non-date questions (实测:C@5/10 63%→48/57)。
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

    score = 2 * term_hits + tag_hits + overlap + table_anchor
    if example.get("aggregate") or example.get("date_range"):
        score = max(1, round(score * 0.6))
    return score


# ── YAML parsing ─────────────────────────────────────────


def _parse_file(path: Path) -> list[tuple[str, str, dict]]:
    """Parse one YAML file into (kind, item_key, payload) entries."""
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    entries: list[tuple[str, str, dict]] = []

    if path.name == "schema_notes.yml":
        for table in data.get("tables", []):
            columns = {}
            enums = {}
            stats = {}
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
                col_stats = col.get("stats")
                if isinstance(col_stats, dict) and col_stats:
                    stats[str(col["name"])] = {
                        k: v for k, v in col_stats.items() if v is not None
                    }
            metrics = {}
            for metric in table.get("metrics", []):
                definition = str(metric.get("definition", "") or "").strip()
                if definition:
                    metrics[str(metric["name"])] = definition
            row_count = table.get("row_count")
            entries.append(("table", str(table["name"]), {
                "description": str(table.get("description", "") or "").strip(),
                "columns": columns,
                "metrics": metrics,
                "enums": enums,
                "stats": stats,
                "row_count": int(row_count) if row_count is not None else None,
            }))

    elif path.name == "semantics.yml":
        # OSSIE semantic_model 格式(见 kb.ossie_format);旧 flat terms: 格式
        # 解析为零条目 + 迁移警告(不兼容决策,需 /kb init --overwrite)。
        for payload in ossie_to_term_payloads(text):
            entries.append(("term", payload["term"], payload))

    elif path.name == "rules.yml":
        for rule in data.get("rules", []):
            text = str(rule.get("rule", "")).strip()
            if text:
                entries.append(("rule", text, {"rule": text}))

    elif path.name == "lessons.yml":
        for lesson in data.get("lessons", []):
            entries.append(("lesson", str(lesson.get("pattern", "")), {
                "pattern": str(lesson.get("pattern", "")),
                "question": str(lesson.get("question", "")),
                "note": str(lesson.get("note", "")),
                "sql_snippet": str(lesson.get("sql_snippet", "")),
                "confirmed": bool(lesson.get("confirmed", False)),
                "upvotes": int(lesson.get("upvotes") or 0),
                "downvotes": int(lesson.get("downvotes") or 0),
            }))

    elif path.name == "examples.yml":
        for example in data.get("examples", []):
            kind = "template" if example.get("template") else "example"
            entries.append((kind, str(example.get("question", "")), {
                "question": str(example.get("question", "")),
                "sql": str(example.get("sql", "")),
                "tags": list(example.get("tags") or []),
                "template": bool(example.get("template")),
                "aggregate": bool(example.get("aggregate")),
                "date_range": bool(example.get("date_range")),
            }))

    return entries


# ── Service ──────────────────────────────────────────────


class KbService:
    """Knowledge base: per-datasource YAML source → SQLite mirror → retrieval.

    The knowledge base is optional: when the .trove/kb directory does
    not exist, every query returns empty and the pipeline behaves
    exactly as without a KB.
    """

    def __init__(self, project_root: str | Path, kb_dir: str | Path | None = None):
        self.kb_dir = (
            Path(kb_dir) if kb_dir is not None
            else Path(project_root) / ".trove" / KB_DIR_NAME
        )
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
            await self._purge_deleted_files(db)
            await db.commit()

    async def _purge_deleted_files(self, db: aiosqlite.Connection) -> None:
        """Drop mirror rows whose source YAML no longer exists on disk.

        Keyed by (datasource, source_file): a same-named file in another
        datasource directory must not keep stale rows alive.
        """
        current = {
            (ds_dir.name, yml.name)
            for ds_dir in self._datasource_dirs()
            for yml in ds_dir.glob("*.yml")
        }
        rows = await (await db.execute(
            "SELECT datasource, source_file FROM kb_items"
        )).fetchall()
        for datasource, source_file in rows:
            if (datasource, source_file) in current:
                continue
            await db.execute(
                "DELETE FROM kb_items WHERE datasource = ? AND source_file = ?",
                (datasource, source_file),
            )
            await db.execute(
                "DELETE FROM kb_sync WHERE file_path = ?",
                (f"{datasource}/{source_file}",),
            )

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
                # default=str:YAML 原生类型(裸日期字面量会还原成 date/
                # datetime)在 JSON 镜像里一律落成字符串,下游 str()/正则
                # 消费方均兼容,否则含日期统计的 schema_notes 让 sync 崩。
                (datasource, kind, item_key,
                 json.dumps(payload, ensure_ascii=False, default=str), yml.name),
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

        async with aiosqlite.connect(self.db_path) as db:
            await self._ensure_mirror_schema(db)
            await db.execute("DELETE FROM kb_sync")
            await self._purge_deleted_files(db)
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
            term = payload["term"]
            if not (
                term in question
                or any(a and a in question for a in payload.get("aliases", []))
                or _term_word_overlap(term, question) >= 0.5
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
        per_table: bool = False,
    ) -> list[ExampleHit]:
        """Top-K reference examples/templates by relevance score.

        With `tables` given, examples that mention OTHER tables are
        dropped (deterministic evidence filtering); examples mentioning
        the matched tables get a score anchor.

        With `per_table=True` and `tables` given, each anchor table gets
        its own top group first (anchored to that table alone), then the
        groups are merged and de-duplicated — a multi-table question keeps
        at least one representative template per matched table instead of
        letting one table's templates crowd out the rest.

        Atomic templates are additionally composed into JOIN × WHERE
        combinations (see kb.compose) before the final top-K cut — a
        multi-table question with a filter sees a structural reference no
        single template covers.
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
        payloads = [json.loads(row["payload"]) for row in rows]

        def keep(payload: dict) -> bool:
            """确定性跨表过滤:示例提到未匹配的表 → 丢弃。"""
            if tables is None or not all_tables:
                return True
            full_text = " ".join([
                str(payload.get("question", "")),
                str(payload.get("sql", "")),
                *[str(t) for t in payload.get("tags", [])],
            ])
            mentioned = [t for t in all_tables if t and t in full_text]
            return not mentioned or any(t in tables for t in mentioned)

        if per_table and tables:
            # 每表分组 top-limit(锚定只给组内表)→ 合并 → 每表保底 1 个,
            # 剩余名额按分填充——多表题的 join 骨架不会因单表高分被挤出
            groups: list[list[ExampleHit]] = []
            merged: dict[str, ExampleHit] = {}
            for t in tables:
                group = [
                    (_score_example(question, matched_terms, p, tables=[t]), p)
                    for p in payloads
                    if keep(p)
                ]
                group = [(s, p) for s, p in group if s > 0]
                group.sort(key=lambda x: x[0], reverse=True)
                hits = [ExampleHit(**p, score=s) for s, p in group[:limit]]
                groups.append(hits)
                for hit in hits:
                    key = f"{hit.sql}|{hit.question}"
                    if key not in merged or merged[key].score < hit.score:
                        merged[key] = hit
            # 保底:每表 top1 先进(表数 > limit 时按表顺序截断)
            picks = [g[0] for g in groups if g]
            picked_keys = {f"{h.sql}|{h.question}" for h in picks}
            rest = [
                h for h in merged.values()
                if f"{h.sql}|{h.question}" not in picked_keys
            ]
            rest.sort(key=lambda h: h.score, reverse=True)
            # 组合只在非保底槽位竞争——保底代表(每表一个)不被组合示例
            # 挤出 top-k(实测:组合参与统一排序会让 C 口径列覆盖回退)
            rest = compose_candidates(
                rest, lang="zh" if _CJK_RE.search(question) else "en")
            rest = [
                hit if isinstance(hit, ExampleHit) else ExampleHit(**hit)
                for hit in rest
            ]
            rest.sort(key=lambda h: h.score, reverse=True)
            return (picks + rest)[:limit]

        scored = []
        for payload in payloads:
            if not keep(payload):
                continue
            score = _score_example(question, matched_terms, payload, tables=tables)
            if score > 0:
                scored.append(ExampleHit(**payload, score=score))
        scored.sort(key=lambda h: h.score, reverse=True)

        # 原子模板组合(JION×WHERE)→ 统一按分排序后截断
        candidates = compose_candidates(
            scored, lang="zh" if _CJK_RE.search(question) else "en")
        candidates = [
            hit if isinstance(hit, ExampleHit) else ExampleHit(**hit)
            for hit in candidates
        ]
        candidates.sort(key=lambda h: h.score, reverse=True)
        return candidates[:limit]

    async def list_templates(self, datasource: str) -> list[ExampleHit]:
        """One datasource's deterministic template rows (kind='template').

        Fast path (fast_match node) input: only kb-init-generated templates
        are visible — compose.py's JOIN×WHERE combination candidates live
        under kind='example' and are excluded by construction (KB
        anti-cheating constraint: the fast path may only emit what kb init
        can generate deterministically).
        """
        if not self.enabled:
            return []
        rows = await self._rows(
            "SELECT payload FROM kb_items WHERE kind = 'template' AND datasource = ? ORDER BY id",
            (datasource,),
        )
        return [ExampleHit(**json.loads(row["payload"])) for row in rows]

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

    async def rate_lesson(self, entry: dict, datasource: str) -> dict:
        """Record a user up/down vote, upserting a lesson keyed by `question`.

        Turns +1 (upvote) or -1 (downvote) into a pending lesson entry; a
        repeat vote on the same question merges into the existing entry and
        re-marks it pending for admin re-review.
        """
        question = entry.get("question")
        path = self.kb_dir / datasource / "lessons.yml"
        data = {}
        if path.exists():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        lessons = list(data.get("lessons", []))
        existing = next((l for l in lessons if l.get("question") == question), None)
        if existing is None:
            lesson = {
                "question": question,
                "note": entry.get("note", ""),
                "sql_snippet": entry.get("sql_snippet", ""),
                "upvotes": 0,
                "downvotes": 0,
                "confirmed": False,
            }
            lessons.append(lesson)
            existing = lesson
        if entry["vote"] == 1:
            existing["upvotes"] = int(existing.get("upvotes", 0)) + 1
        else:
            existing["downvotes"] = int(existing.get("downvotes", 0)) + 1
        existing["confirmed"] = False
        if entry.get("sql_snippet"):
            existing["sql_snippet"] = entry["sql_snippet"]
        data["lessons"] = lessons
        path.write_text(
            yaml.safe_dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        await self.force_sync()
        return dict(existing)

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

    async def get_lesson(self, datasource: str, pattern: str) -> dict | None:
        """One lesson by exact pattern match, or None."""
        path = self.kb_dir / datasource / "lessons.yml"
        if not path.exists():
            return None
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for lesson in data.get("lessons", []):
            if lesson.get("pattern") == pattern:
                return lesson
        return None

    async def confirm_lesson(self, datasource: str, pattern: str) -> bool:
        """Confirm one pending lesson by pattern (rewrites the YAML).

        Returns False when the pattern is absent. Idempotent for an
        already-confirmed lesson (returns True).
        """
        path = self.kb_dir / datasource / "lessons.yml"
        if not path.exists():
            return False
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        found = False
        for lesson in data.get("lessons", []):
            if lesson.get("pattern") == pattern:
                lesson["confirmed"] = True
                found = True
                break
        if not found:
            return False
        path.write_text(
            yaml.safe_dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        await self.force_sync(datasource)
        return True

    async def reject_lesson(self, datasource: str, pattern: str) -> bool:
        """Remove one lesson by pattern (rewrites the YAML).

        Returns False when the pattern is absent.
        """
        path = self.kb_dir / datasource / "lessons.yml"
        if not path.exists():
            return False
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        lessons = data.get("lessons", [])
        before = len(lessons)
        data["lessons"] = [l for l in lessons if l.get("pattern") != pattern]
        if len(data["lessons"]) == before:
            return False
        path.write_text(
            yaml.safe_dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        await self.force_sync(datasource)
        return True

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

    async def kb_status(self, datasource: str) -> dict:
        """Init/state summary of one datasource's KB (admin API)."""
        files = self.init_exists(datasource)
        items = (await self.list_items()).get(datasource, {})
        return {"initialized": bool(files), "files": files, "items": items}

    # ── Evolution (human-confirmed writes) ────────────────

    async def append_example(self, entry: dict[str, Any], datasource: str) -> None:
        """Append a reference-SQL/template entry to the datasource's examples.yml."""
        await self._append_entry("examples.yml", "examples", entry, datasource)

    async def append_term(self, entry: dict[str, Any], datasource: str) -> None:
        """Append a business term to the datasource's semantics.yml (OSSIE format).

        flat 请求体(term/aliases/mapping/tables/definition)在此转换为
        OSSIE metric 追加进 semantic_model。守卫:
        - 空 mapping 拒绝(无表达式的 metric 会被 OSSIE 解析器整体丢弃);
        - 旧 flat terms: 文件拒绝写入(不兼容决策,需 /kb init --overwrite)。
        """
        mapping = str(entry.get("mapping", "") or "").strip()
        if not mapping:
            raise ValueError(
                "term mapping is required: expressionless metrics are dropped "
                "by the OSSIE parser")
        entry = dict(entry)
        entry["mapping"] = qualify_mapping(mapping, list(entry.get("tables") or []))

        ds_dir = self.kb_dir / datasource
        ds_dir.mkdir(parents=True, exist_ok=True)
        path = ds_dir / "semantics.yml"
        if path.exists():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(data, dict) or "semantic_model" not in data:
                raise ValueError(
                    "semantics.yml uses the legacy flat terms format — delete the "
                    "file and re-run /kb init --overwrite to migrate")
            append_term_to_document(data, entry)
        else:
            data = terms_to_ossie_document([entry], model_name=datasource)
        path.write_text(
            yaml.safe_dump(
                data, default_flow_style=False, allow_unicode=True, sort_keys=False,
            ),
            encoding="utf-8",
        )
        await self.force_sync()

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
        """Write a semantics.yml as an OSSIE semantic_model (LLM-assisted /kb init)."""
        doc = terms_to_ossie_document(terms, model_name=datasource)
        return self._init_file(
            "semantics.yml", "semantic_model", doc["semantic_model"], datasource, overwrite)

    def init_examples(
        self, examples: list[dict], datasource: str, overwrite: bool = False,
    ) -> bool:
        """Write an examples.yml (LLM-assisted /kb init)."""
        return self._init_file("examples.yml", "examples", examples, datasource, overwrite)
