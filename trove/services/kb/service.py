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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import aiosqlite
import yaml

from trove.core.logging import get_logger

_CJK_RE = re.compile(r"[一-鿿]")
from trove.core.types import SchemaInfo
from trove.services.kb.backends.dense import CREATE_VECTORS
from trove.services.kb.backends.fts import fts_item_text
from trove.services.kb.compose import compose_candidates
from trove.services.kb.embeddings import (
    coverage_score,
    near_duplicate,
    rerank_score,
)
from trove.services.kb.ossie_format import (
    append_term_to_document,
    ossie_to_entity_payloads,
    ossie_to_metric_payloads,
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

# kb_items 的 FTS5 稀疏镜像(contentless + UNINDEXED 元数据列)。
# rowid = kb_items.id;text 为预分词后的可检索文本(见 backends.fts)。
# 与 kb_items 同事务同步,删除传播天然成立。
_CREATE_FTS = """CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts USING fts5(
    text,
    datasource UNINDEXED,
    kind UNINDEXED,
    source_file UNINDEXED
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
    score: float = 0.0
    aggregate: bool = False
    date_range: bool = False


@dataclass
class MetricHit:
    """A semantic metric matched against the question (typed retrieval)."""

    name: str
    aliases: list[str] = field(default_factory=list)
    definition: str = ""
    expression: str = ""
    datasets: list[str] = field(default_factory=list)
    score: float = 0.0


@dataclass
class EntityHit:
    """A semantic dimension/entity field matched against the question.

    ``enum_values``/``enum_labels`` 是值确认通道:code → 人类 label 的映射
    (值落在字段层,不参与可答性判定,见 schema_linking 的语义优先边界)。
    """

    field: str
    dataset: str = ""
    role: str = ""
    description: str = ""
    synonyms: list[str] = field(default_factory=list)
    enum_values: list[str] = field(default_factory=list)
    enum_labels: list[str] = field(default_factory=list)
    score: float = 0.0


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


def _char_overlap(text: str, question: str) -> float:
    """字符二元组覆盖:``text`` 的 bigram 有多少出现在问题里(中英同用)。

    与 ``_term_word_overlap`` 互补——后者对纯中文失效(词元切分不到),
    前者用字符 bigram,专治中文近义("地区名"↔"哪个地区")。作为弱门信号:
    描述/枚举文本里 ≥50% 的 bigram 出现在问题即视为命中。
    """
    bs = _bigrams(text or "")
    if not bs:
        return 0.0
    return sum(1 for b in bs if b in (question or "")) / len(bs)


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
    if (example.get("aggregate") or example.get("date_range")) and score > 0:
        score = max(1, round(score * 0.6))
    return score


def _score_metric(question: str, payload: dict) -> float | None:
    """类型加权指标打分:名称命中 ×3 > 别名 ×2 > 定义重叠 > coverage 门内。

    确定性门 = 名称/别名子串 · 词重叠 · 定义词/字符重叠(coverage 只在
    门内提升近义候选排序)——检索信号不改变"零确定性命中 = 不返回"。
    """
    name = str(payload.get("name") or "")
    aliases = [str(a) for a in payload.get("aliases", []) or []]
    definition = str(payload.get("definition") or "")
    name_hit = 1.0 if (
        name in question or _term_word_overlap(name, question) >= 0.5
    ) else 0.0
    alias_hit = 1.0 if any(a and a in question for a in aliases) else 0.0
    def_overlap = max(
        _term_word_overlap(definition, question) if definition else 0.0,
        _char_overlap(definition, question) if definition else 0.0,
    )
    index_text = " ".join([name, *aliases, definition, str(payload.get("expression") or "")])
    sim = coverage_score(question, index_text)
    if name_hit == 0.0 and alias_hit == 0.0 and def_overlap < 0.5 and sim < 0.45:
        return None
    return 3.0 * name_hit + 2.0 * alias_hit + 1.5 * def_overlap + sim


def _score_entity(question: str, payload: dict) -> float | None:
    """类型加权实体打分:字段/同义词/枚举命中各 ×2 + 描述/覆盖率门内。

    identifier/time 结构列(含 is_time)的原始名子串匹配排除(撞普通词,
    与 schema_linking._semantic_match_datasets 同哲学);同义词/枚举值/
    中文描述重叠不受此限(人工业务词表)。
    """
    field = str(payload.get("field") or "")
    synonyms = [str(s) for s in payload.get("synonyms", []) or []]
    enums = [str(e) for e in payload.get("enum_values", []) or []]
    enum_labels = [str(l) for l in payload.get("enum_labels", []) or []]
    description = str(payload.get("description") or "")
    role = str(payload.get("role") or "").strip().lower()
    structural = role in ("identifier", "time") or bool(payload.get("is_time"))
    field_hit = bool(field) and field in question and not structural
    syn_hit = any(s and s in question for s in synonyms)
    enum_hit = any(
        (e and e in question) or (l and l in question)
        for e, l in zip(enums, enum_labels)
    ) or any(e and e in question for e in enums)
    desc_overlap = _char_overlap(description, question) if description else 0.0
    index_text = " ".join([
        field, *synonyms, description, *enums, *enum_labels,
        str(payload.get("dataset") or ""),
    ])
    sim = coverage_score(question, index_text)
    if not (field_hit or syn_hit or enum_hit) and desc_overlap < 0.5 and sim < 0.45:
        return None
    return (
        2.0 * (1.0 if field_hit else 0.0)
        + 2.0 * (1.0 if syn_hit else 0.0)
        + 2.0 * (1.0 if enum_hit else 0.0)
        + desc_overlap
        + sim
    )


def _lesson_table_ok(lesson: dict, matched: list[str], all_tables: list[str]) -> bool:
    """Hint Bank 经验按表锚过滤：提到未匹配表的教训与当前问题无关。"""
    if not matched or not all_tables:
        return True
    text = " ".join([
        str(lesson.get("pattern", "")),
        str(lesson.get("note", "")),
        str(lesson.get("sql_snippet", "")),
    ])
    mentioned = [t for t in all_tables if t and t in text]
    return not mentioned or any(t in matched for t in mentioned)


def _lesson_text(lesson: dict) -> str:
    return " ".join([
        str(lesson.get("pattern", "")),
        str(lesson.get("note", "")),
        str(lesson.get("sql_snippet", "")),
    ])


def _recency_factor(ts: str | None, half_life_days: int = 365) -> float:
    """时效衰减因子:有时间戳则 0.9~1.0(一年半衰,最多少 10%),无则 1.0。

    只做温和的排序微调,不让久远教训被挤出,避免正确历史被过度惩罚。
    """
    if not ts:
        return 1.0
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        days = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400)
    except Exception:
        return 1.0
    return 0.9 + 0.1 * (0.5 ** (days / half_life_days))


def _fuse_extra_sim(sim: float, extra: float, alpha: float = 0.5) -> float:
    """通道相似度(BM25/RRF)与确定性覆盖度融合(混合检索门内排序)。

    仅 hybrid/rag 后端传入 sim_scores 时生效;alpha 使通道分与 coverage
    同量级,不压过表锚/词重叠这些高区分度确定性信号(与 rerank_score 的
    weight 同哲学:检索信号只排序、不改变"零确定性命中 = 不返回")。
    """
    return alpha * sim + (1.0 - alpha) * extra


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
        # 分型导出:term(子串检索)/ metric(指标相关性)/ entity(维度/枚举值)
        # ——三套镜像条目共享同一份解析,降级一致。
        for payload in ossie_to_term_payloads(text):
            entries.append(("term", payload["term"], payload))
        for payload in ossie_to_metric_payloads(text):
            entries.append(("metric", payload["name"], payload))
        for payload in ossie_to_entity_payloads(text):
            entries.append((
                "entity", f"{payload['dataset']}.{payload['field']}", payload,
            ))

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

    def __init__(
        self,
        project_root: str | Path,
        kb_dir: str | Path | None = None,
        backend_resolver: Callable[[str], Any] | None = None,
    ):
        self.kb_dir = (
            Path(kb_dir) if kb_dir is not None
            else Path(project_root) / ".trove" / KB_DIR_NAME
        )
        self.db_path = self.kb_dir / "kb.sqlite"
        # 按数据源解析检索后端(builtin → None 走本类现有逻辑);
        # 检索失败/未配置一律退化 builtin,不阻断生成。
        self._backend_resolver = backend_resolver
        # /kb learn draft awaiting user confirmation
        self.pending_draft: dict[str, Any] | None = None

    def _backend_for(self, datasource: str):
        """该数据源的检索后端;builtin/未配置/解析失败 → None。"""
        if self._backend_resolver is None:
            return None
        try:
            return self._backend_resolver(datasource or "")
        except Exception:
            return None

    @property
    def enabled(self) -> bool:
        return self.kb_dir.is_dir()

    def semantics_path(self, datasource: str) -> Path:
        """该数据源的 OSSIE 语义模型文件(语义层单一真源)。"""
        return self.kb_dir / datasource / "semantics.yml"

    def schema_notes_path(self, datasource: str) -> Path:
        """该数据源的 schema_notes.yml 文件(表/字段/指标口径注释)。"""
        return self.kb_dir / datasource / "schema_notes.yml"

    # ── Sync ──────────────────────────────────────────────

    async def iter_items(
        self, datasource: str,
    ) -> list[dict]:
        """Yield all KB items for a datasource as dicts.

        Each dict: ``{"kind", "item_key", "payload", "source_file"}`` (payload
        parsed from JSON). Used by the hybrid-retrieval indexer to build
        retrievable documents from the parsed mirror.
        """
        import json

        out: list[dict] = []
        if not self.enabled:
            return out
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT kind, item_key, payload, source_file FROM kb_items "
                "WHERE datasource = ?",
                (datasource,),
            )
            for r in await cur.fetchall():
                try:
                    payload = json.loads(r["payload"])
                except Exception:
                    continue
                out.append({
                    "kind": r["kind"],
                    "item_key": r["item_key"],
                    "payload": payload,
                    "source_file": r["source_file"],
                })
        return out

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

        purged: list[tuple[str, str]] = []
        async with aiosqlite.connect(self.db_path) as db:
            await self._ensure_mirror_schema(db)
            for ds_dir in sorted(self._datasource_dirs()):
                for yml in sorted(ds_dir.glob("*.yml")):
                    await self._sync_file(db, yml, ds_dir.name)
            purged = await self._purge_deleted_files(db)
            await db.commit()
        # 向量清理放镜像事务外(delete_file 走独立连接,避免锁竞争)
        for datasource, source_file in purged:
            await self._delete_vectors(datasource, source_file)

    async def _purge_deleted_files(
        self, db: aiosqlite.Connection,
    ) -> list[tuple[str, str]]:
        """Drop mirror rows whose source YAML no longer exists on disk.

        Keyed by (datasource, source_file): a same-named file in another
        datasource directory must not keep stale rows alive.

        Returns the purged (datasource, source_file) pairs so the caller
        can clean derived mirrors (vectors) outside this transaction.
        """
        current = {
            (ds_dir.name, yml.name)
            for ds_dir in self._datasource_dirs()
            for yml in ds_dir.glob("*.yml")
        }
        rows = await (await db.execute(
            "SELECT datasource, source_file FROM kb_items"
        )).fetchall()
        purged: list[tuple[str, str]] = []
        for datasource, source_file in rows:
            if (datasource, source_file) in current:
                continue
            await db.execute(
                "DELETE FROM kb_items WHERE datasource = ? AND source_file = ?",
                (datasource, source_file),
            )
            await db.execute(
                "DELETE FROM kb_fts WHERE datasource = ? AND source_file = ?",
                (datasource, source_file),
            )
            await db.execute(
                "DELETE FROM kb_sync WHERE file_path = ?",
                (f"{datasource}/{source_file}",),
            )
            purged.append((datasource, source_file))
        return purged

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
        await self._sync_file_fts(db, datasource, yml.name, entries)
        await db.execute(
            "INSERT OR REPLACE INTO kb_sync (file_path, mtime) VALUES (?, ?)",
            (rel_path, mtime),
        )
        await db.commit()
        # 向量索引在镜像事务外(embedding 是慢 LLM 调用,不阻塞镜像写事务;
        # 失败降级,稀疏通道仍可用)。
        await self._index_vectors_for_file(datasource, yml.name, entries)

    async def _index_vectors_for_file(
        self, datasource: str, source_file: str,
        entries: list[tuple[str, str, dict]],
    ) -> None:
        """按后端重建该文件的向量索引(rag 的 index_file 钩子;其余 no-op)。"""
        backend = self._backend_for(datasource)
        indexer = getattr(backend, "index_file", None)
        if indexer is None:
            return
        try:
            await indexer(datasource, source_file, entries)
        except Exception:
            logger.warning(
                "KB vector indexing failed for %s/%s; sparse channel still serves",
                datasource, source_file, exc_info=True,
            )

    async def _delete_vectors(self, datasource: str, source_file: str) -> None:
        """删除传播:文件从磁盘移除时清理向量(rag 钩子;其余 no-op)。"""
        backend = self._backend_for(datasource)
        deleter = getattr(backend, "delete_file", None)
        if deleter is None:
            return
        try:
            await deleter(datasource, source_file)
        except Exception:
            logger.warning(
                "KB vector delete failed for %s/%s", datasource, source_file,
                exc_info=True,
            )

    async def _clear_vectors(self, datasource: str) -> None:
        """delete_kb 时清空该数据源向量(rag 钩子;其余 no-op)。"""
        backend = self._backend_for(datasource)
        clearer = getattr(backend, "clear", None)
        if clearer is None:
            return
        try:
            await clearer(datasource)
        except Exception:
            logger.warning(
                "KB vector clear failed for %s", datasource, exc_info=True)

    async def _sync_file_fts(
        self, db: aiosqlite.Connection, datasource: str, source_file: str,
        entries: list[tuple[str, str, dict]],
    ) -> None:
        """重建该文件的 kb_fts 镜像(与 kb_items 同事务,删除传播天然)。

        kb_items 先删除后按序重插,id 连续递增——ORDER BY id 取回的新
        id 与 entries 顺序一一对应,逐条写入 FTS 索引。
        """
        await db.execute(
            "DELETE FROM kb_fts WHERE datasource = ? AND source_file = ?",
            (datasource, source_file),
        )
        rows = await (await db.execute(
            "SELECT id FROM kb_items WHERE datasource = ? AND source_file = ? "
            "ORDER BY id",
            (datasource, source_file),
        )).fetchall()
        for (id_,), (kind, _key, payload) in zip(rows, entries):
            text = fts_item_text(kind, payload)
            if not text:
                continue
            await db.execute(
                "INSERT INTO kb_fts (rowid, text, datasource, kind, source_file) "
                "VALUES (?, ?, ?, ?, ?)",
                (id_, text, datasource, kind, source_file),
            )

    async def force_sync(self, default_datasource: str | None = None) -> None:
        """Reload every YAML file unconditionally (/kb reload).

        Also purges mirror items whose source file was deleted from disk.
        """
        if not self.enabled:
            return
        self.kb_dir.mkdir(parents=True, exist_ok=True)
        self._migrate_legacy_files(default_datasource)

        purged: list[tuple[str, str]] = []
        async with aiosqlite.connect(self.db_path) as db:
            await self._ensure_mirror_schema(db)
            await db.execute("DELETE FROM kb_sync")
            purged = await self._purge_deleted_files(db)
            await db.commit()
        for datasource, source_file in purged:
            await self._delete_vectors(datasource, source_file)
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
        """Create mirror tables; rebuild if the schema predates a feature.

        Rebuild triggers: missing ``datasource`` column (pre-multi-datasource)
        or missing ``kb_fts`` virtual table (pre-FTS mirrors). Old mirrors that
        already carry the datasource column but predate kb_fts would otherwise
        keep a broken write path (INSERT INTO kb_fts → no such table).
        """
        cursor = await db.execute("PRAGMA table_info(kb_items)")
        columns = {row[1] for row in await cursor.fetchall()}
        fts_cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='kb_fts'")
        has_fts = (await fts_cursor.fetchone()) is not None
        if columns and (("datasource" not in columns) or not has_fts):
            logger.info("Rebuilding KB mirror (missing datasource column / kb_fts)")
            await db.execute("DROP TABLE kb_items")
            await db.execute("DROP TABLE IF EXISTS kb_sync")
            await db.execute("DROP TABLE IF EXISTS kb_fts")
            await db.execute("DROP TABLE IF EXISTS kb_vectors")
            columns = set()
        if not columns:
            await db.execute(_CREATE_ITEMS)
            await db.execute(_CREATE_SYNC)
            await db.execute(_CREATE_FTS)
            await db.execute(CREATE_VECTORS)
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
        """Terms whose term/alias matches the question (per-datasource backend).

        按数据源配置的检索后端读时 dispatch;builtin(默认)= 子串/词重叠
        确定性匹配。后端解析失败一律退化 builtin,不阻断检索。
        """
        backend = self._backend_for(datasource)
        if backend is not None:
            return await backend.search_terms(
                question, datasource, tables=tables, all_tables=all_tables)
        return await self._search_terms(question, datasource, tables, all_tables)

    async def search_schema_docs(
        self,
        question: str,
        datasource: str,
        limit: int = 5,
    ) -> list[Any]:
        """检索已索引的物理 schema 元数据(schema_doc),用于查询时表/列锚定。

        仅当数据源后端为 pg_hybrid(统一 PG 检索库)时返回非空;其他后端
        (builtin/hybrid/rag)无 schema_doc 通道,返回空,不影响语义优先边界。
        """
        backend = self._backend_for(datasource)
        if backend is None or not hasattr(backend, "search_schema_docs"):
            return []
        try:
            return await backend.search_schema_docs(question, datasource, limit=limit)
        except Exception as e:
            logger.warning("schema_doc retrieval failed (%s): %s", datasource, e)
            return []

    async def search_metrics(
        self,
        question: str,
        datasource: str,
        tables: list[str] | None = None,
        all_tables: list[str] | None = None,
        limit: int = 5,
    ) -> list[MetricHit]:
        """Top-K 语义指标检索(typed corpus):名称/别名/定义词法门 → coverage 门内。

        词法门决定"是否返回",类型加权分排序(名称×3 > 别名×2 > 定义重叠
        > coverage);``tables`` 给定后,绑定到未匹配数据集的指标被丢弃
        (与 search_terms 同语义)。向量不参与名称匹配(与 term 同哲学),
        因此不经过检索后端 dispatch——直接读 kind='metric' 镜像。
        """
        if not self.enabled:
            return []
        rows = await self._rows(
            "SELECT payload FROM kb_items WHERE kind = 'metric' AND datasource = ?",
            (datasource,),
        )
        hits: list[MetricHit] = []
        for row in rows:
            payload = json.loads(row["payload"])
            name = str(payload.get("name") or "")
            if not name:
                continue
            if tables is not None:
                bound = list(payload.get("datasets") or [])
                if bound and not any(t in tables for t in bound):
                    continue
            score = _score_metric(question, payload)
            if score is None:
                continue
            hits.append(MetricHit(
                name=name,
                aliases=list(payload.get("aliases") or []),
                definition=str(payload.get("definition") or ""),
                expression=str(payload.get("expression") or ""),
                datasets=list(payload.get("datasets") or []),
                score=round(score, 4),
            ))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:limit]

    async def search_entities(
        self,
        question: str,
        datasource: str,
        tables: list[str] | None = None,
        all_tables: list[str] | None = None,
        limit: int = 8,
    ) -> list[EntityHit]:
        """Top-K 维度/枚举实体检索(typed corpus):字段/同义词/枚举值词法门。

        ``tables`` 给定后,只返回属于这些数据集的实体(图链接的
        沿边拉取入口);identifier/time 结构列不做原始名子串匹配。
        """
        if not self.enabled:
            return []
        rows = await self._rows(
            "SELECT payload FROM kb_items WHERE kind = 'entity' AND datasource = ?",
            (datasource,),
        )
        hits: list[EntityHit] = []
        for row in rows:
            payload = json.loads(row["payload"])
            if tables is not None:
                ds = str(payload.get("dataset") or "")
                if ds and ds not in tables:
                    continue
            score = _score_entity(question, payload)
            if score is None:
                continue
            hits.append(EntityHit(
                field=str(payload.get("field") or ""),
                dataset=str(payload.get("dataset") or ""),
                role=str(payload.get("role") or ""),
                description=str(payload.get("description") or ""),
                synonyms=list(payload.get("synonyms") or []),
                enum_values=list(payload.get("enum_values") or []),
                enum_labels=list(payload.get("enum_labels") or []),
                score=round(score, 4),
            ))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:limit]

    async def metric_family(
        self,
        question: str,
        datasource: str,
        matched_tables: list[str] | None = None,
        all_tables: list[str] | None = None,
        metric_limit: int = 4,
        entity_limit: int = 8,
    ) -> dict[str, Any]:
        """图链接:metric 命中 → 沿 metric.datasets 拉 entity 族 + 扩展表锚。

        返回 ``{"metrics", "entities", "tables"}``:metrics 是相关性选择的
        指标(不做过窄表过滤,保证召回);tables = matched_tables ∪ 各指标
        锚定的数据集(已在语义模型内声明,不越语义优先边界);entities 只
        取这些数据集里与问题词法相关的维度/枚举字段(值确认通道)。
        """
        metrics = await self.search_metrics(
            question, datasource, tables=None, all_tables=all_tables,
            limit=metric_limit,
        )
        tables = list(matched_tables or [])
        metric_tables: list[str] = []
        for m in metrics:
            for d in m.datasets:
                if d and d not in tables:
                    tables.append(d)
                if d and d not in metric_tables:
                    metric_tables.append(d)
        entities = await self.search_entities(
            question, datasource, tables=metric_tables or None,
            all_tables=all_tables, limit=entity_limit,
        )
        return {"metrics": metrics, "entities": entities, "tables": tables}

    async def _search_terms(
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

        按数据源配置的检索后端读时 dispatch;builtin(默认)= 确定性分 +
        hashed n-gram 重排;hybrid = FTS5/BM25 召回 + 门内融合重排。

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
        backend = self._backend_for(datasource)
        if backend is not None:
            return await backend.search_examples(
                question, datasource, limit=limit,
                tables=tables, all_tables=all_tables, per_table=per_table)
        return await self._search_examples(
            question, datasource, limit, tables, all_tables, per_table)

    async def _search_examples(
        self,
        question: str,
        datasource: str,
        limit: int = 3,
        tables: list[str] | None = None,
        all_tables: list[str] | None = None,
        per_table: bool = False,
    ) -> list[ExampleHit]:
        """builtin 示例检索:全量候选集上跑确定性排序。"""
        if not self.enabled:
            return []
        rows = await self._rows(
            "SELECT id, payload FROM kb_items "
            "WHERE kind IN ('example', 'template') AND datasource = ? ORDER BY id",
            (datasource,),
        )
        items = [(r["id"], json.loads(r["payload"])) for r in rows]
        return await self._rank_examples(
            question, datasource, items, limit, tables, all_tables, per_table)

    async def _rank_examples(
        self,
        question: str,
        datasource: str,
        items: list[tuple[int, dict]],
        limit: int,
        tables: list[str] | None,
        all_tables: list[str] | None,
        per_table: bool,
        sim_scores: dict[int, float] | None = None,
    ) -> list[ExampleHit]:
        """示例候选排序(builtin 全量集 / hybrid·rag 召回子集共用)。

        Args:
            items: [(kb_items.rowid, payload)] 候选。
            sim_scores: 可选 {rowid: 0..1} 通道相似度(hybrid = BM25,rag =
                RRF),门内融合进 rerank sim(builtin 为 None 保持现状)。
        """
        if not self.enabled:
            return []
        term_hits = await self._search_terms(question, datasource)
        matched_terms = {h.term for h in term_hits}

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

        def _sim(payload: dict, id_: int) -> float:
            sim = coverage_score(question, str(payload.get("question", "")))
            if sim_scores is not None:
                sim = _fuse_extra_sim(sim, sim_scores.get(id_, 0.0))
            return sim

        if per_table and tables:
            # 每表分组 top-limit(锚定只给组内表)→ 合并 → 每表保底 1 个,
            # 剩余名额按分填充——多表题的 join 骨架不会因单表高分被挤出。
            # 混合检索:确定性分(表锚/词重叠)是硬门,相似度(embedding/
            # BM25)在门内提升近义候选的排序(无关候选 det=0 仍被排除)。
            groups: list[list[ExampleHit]] = []
            merged: dict[str, ExampleHit] = {}
            for t in tables:
                group = []
                for id_, p in items:
                    if not keep(p):
                        continue
                    det = _score_example(question, matched_terms, p, tables=[t])
                    if det <= 0:
                        continue
                    group.append((rerank_score(det, _sim(p, id_)), p))
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
        for id_, p in items:
            if not keep(p):
                continue
            det = _score_example(question, matched_terms, p, tables=tables)
            if det <= 0:
                continue
            scored.append(ExampleHit(**p, score=rerank_score(det, _sim(p, id_))))
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

    async def search_lessons(
        self,
        question: str,
        datasource: str,
        limit: int = 3,
        tables: list[str] | None = None,
        all_tables: list[str] | None = None,
    ) -> list[dict]:
        """Hint Bank 语义检索(按数据源后端 dispatch)。

        替代原 graphs 里的纯子串过滤(``pattern in question``)——近义改写
        (中英文/同义表述)也能命中;表锚过滤保留(提到未匹配表的教训丢弃)。
        ``score`` 写入每条返回 dict 供 context budget 排序。
        """
        backend = self._backend_for(datasource)
        if backend is not None:
            return await backend.search_lessons(
                question, datasource, limit=limit,
                tables=tables, all_tables=all_tables)
        return await self._search_lessons(question, datasource, limit, tables, all_tables)

    async def _search_lessons(
        self,
        question: str,
        datasource: str,
        limit: int = 3,
        tables: list[str] | None = None,
        all_tables: list[str] | None = None,
    ) -> list[dict]:
        """builtin 教训检索:全量教训(仅 confirmed)上做相似度排序。"""
        if not self.enabled:
            return []
        lessons = await self.list_lessons(datasource)
        items = [(i, l) for i, l in enumerate(lessons)]
        return await self._rank_lessons(
            question, datasource, items, limit, tables, all_tables)

    async def _rank_lessons(
        self,
        question: str,
        datasource: str,
        items: list[tuple[int, dict]],
        limit: int,
        tables: list[str] | None,
        all_tables: list[str] | None,
        sim_scores: dict[int, float] | None = None,
    ) -> list[dict]:
        """教训候选排序(builtin 全量集 / hybrid·rag 召回子集共用)。

        ``score`` = 相似度(coverage × 可选通道分) × 投票加权 × 时效衰减。
        """
        scored: list[dict] = []
        for item_id, lesson in items:
            if not _lesson_table_ok(lesson, tables, all_tables):
                continue
            sim = coverage_score(question, _lesson_text(lesson))
            if sim <= 0:
                continue
            if sim_scores is not None:
                sim = _fuse_extra_sim(sim, sim_scores.get(item_id, 0.0))
            votes = int(lesson.get("upvotes", 0) or 0) - int(lesson.get("downvotes", 0) or 0)
            lesson = dict(lesson)
            lesson["score"] = sim * (1 + 0.25 * votes) * _recency_factor(
                lesson.get("updated_at") or lesson.get("created_at"))
            scored.append(lesson)
        scored.sort(key=lambda l: l.get("score", 0.0), reverse=True)
        return scored[:limit]

    async def append_lesson(self, entry: dict, datasource: str) -> None:
        """Record a lesson candidate (pending until confirmed).

        教训卫生:与已存在的教训(含 pending)近义重复(embedding ≥ 阈值)或
        同 pattern 时跳过写入,避免 Hint Bank 无限膨胀。新教训带 created_at。
        去重直接读 lessons.yml(写路径与镜像解耦,不依赖 sync)。
        """
        entry.setdefault("confirmed", False)
        if not entry.get("created_at"):
            entry["created_at"] = datetime.now(timezone.utc).isoformat()
        path = self.kb_dir / datasource / "lessons.yml"
        existing: list[dict] = []
        if path.exists():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            existing = list(data.get("lessons", []))
        pattern = str(entry.get("pattern", "")).strip()
        new_text = _lesson_text(entry)
        for lesson in existing:
            if pattern and str(lesson.get("pattern", "")).strip() == pattern:
                logger.info("Lesson dedup: identical pattern %r — skipped", pattern)
                return
            if near_duplicate(new_text, _lesson_text(lesson)):
                logger.info("Lesson dedup: near-duplicate of existing lesson — skipped")
                return
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
        existing["updated_at"] = datetime.now(timezone.utc).isoformat()
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
        """Init/state summary of one datasource's KB (admin API).

        ``initialized`` 与列表页一致:三个关键文件**全部**存在才算完成
        (半成品允许续跑补齐,不让 UI 卡在"已初始化"却缺文件)。
        """
        files = self.init_exists(datasource)
        items = (await self.list_items()).get(datasource, {})
        return {
            "initialized": len(files) == 3,
            "files": files,
            "items": items,
        }

    async def list_term_entries(self, datasource: str) -> list[dict]:
        """Full term payloads of one datasource (management UI)."""
        if not self.enabled:
            return []
        rows = await self._rows(
            "SELECT payload FROM kb_items WHERE kind = 'term' AND datasource = ? "
            "ORDER BY id",
            (datasource,),
        )
        return [json.loads(row["payload"]) for row in rows]

    async def list_example_entries(self, datasource: str) -> list[dict]:
        """Full example/template payloads of one datasource (management UI)."""
        if not self.enabled:
            return []
        rows = await self._rows(
            "SELECT payload FROM kb_items "
            "WHERE kind IN ('example', 'template') AND datasource = ? ORDER BY id",
            (datasource,),
        )
        return [json.loads(row["payload"]) for row in rows]

    async def kb_detail(self, datasource: str) -> dict:
        """One aggregate dump for the KB management page (admin API).

        Reads go through the mirror, so a fresh ``force_sync``/``ensure_synced``
        must run first (the admin endpoint does it) to reflect YAML changes.
        """
        terms = await self.list_term_entries(datasource)
        examples = await self.list_example_entries(datasource)
        rules = await self.list_rules(datasource)
        lessons = await self.list_lessons(datasource, confirmed_only=False)
        return {
            "status": await self.kb_status(datasource),
            "terms": terms,
            "examples": examples,
            "rules": rules,
            "lessons": lessons,
        }

    async def delete_kb(self, datasource: str) -> None:
        """Delete a datasource's KB: files + mirror rows + init lock.

        ``datasource`` is used as a directory name, so path-safety is
        enforced here (not just at the API) — the KB dir must never
        escape ``kb_dir``.
        """
        from trove.services.datasource.naming import is_path_safe

        if not is_path_safe(datasource):
            raise ValueError(f"unsafe KB datasource name {datasource!r}")
        ds_dir = self.kb_dir / datasource
        if ds_dir.exists():
            shutil.rmtree(ds_dir)
        # 清理该数据源在镜像(sync 与 items)中的残留行。
        if self.db_path.exists():
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "DELETE FROM kb_items WHERE datasource = ?", (datasource,))
                await db.execute(
                    "DELETE FROM kb_fts WHERE datasource = ?", (datasource,))
                await db.execute(
                    "DELETE FROM kb_sync WHERE file_path LIKE ?", (f"{datasource}/%",))
                await db.commit()
        await self._clear_vectors(datasource)

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

    def kb_initialized(self, datasource: str) -> bool:
        """语义层/查询是否真正就绪:三个关键文件**全部**存在。

        只用"任一存在"(init_exists)会把半成品 init(如中断后只有
        schema_notes.yml)误判为已初始化——UI 切到重新同步却生成不了
        缺失文件。全部齐全才算完成;半成品允许重跑续补。
        """
        return len(self.init_exists(datasource)) == 3

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

    def _init_doc(
        self, filename: str, doc: dict, datasource: str,
        overwrite: bool = False,
    ) -> bool:
        """Write a full YAML document as-is(顶层键保留,如 OSSIE ``version``)。"""
        ds_dir = self.kb_dir / datasource
        ds_dir.mkdir(parents=True, exist_ok=True)
        path = ds_dir / filename
        if path.exists() and not overwrite:
            logger.info("%s/%s already exists; refusing to overwrite", datasource, filename)
            return False
        path.write_text(
            yaml.safe_dump(
                doc, default_flow_style=False, allow_unicode=True, sort_keys=False,
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
        return self._init_doc(
            "semantics.yml", doc, datasource, overwrite)

    def init_semantics(
        self, doc: dict, datasource: str, overwrite: bool = False,
    ) -> bool:
        """Write a semantics.yml from a full OSSIE semantic_model document.

        ``doc`` carries the deterministic structure layer
        (datasets with fields/primary keys + relationships) plus metrics —
        a superset of what ``init_terms`` writes. See semantic_gen.py.
        顶层键(如 OSSIE ``version``)原样写盘(完整文档)。
        """
        return self._init_doc(
            "semantics.yml", doc, datasource, overwrite)

    def init_examples(
        self, examples: list[dict], datasource: str, overwrite: bool = False,
    ) -> bool:
        """Write an examples.yml (LLM-assisted /kb init)."""
        return self._init_file("examples.yml", "examples", examples, datasource, overwrite)
