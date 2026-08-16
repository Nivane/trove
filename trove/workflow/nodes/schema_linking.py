"""Schema Linking node — identifies relevant tables for a query.

Two matching sources:
  1. Knowledge base terms: business terms (中文子串/alias 匹配) whose
     tables join the match set — this is what makes Chinese questions work
  2. Datasource catalog: table/column name search (ASCII tokens)

Matched tables get their human annotations (table description, column
descriptions, metric definitions) merged into the schema context.

Node shape: `async def schema_linking(state: WorkflowState) -> dict`
returns a partial state update.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any

from trove.services.datasource.catalog import CatalogService
from trove.services.datasource.registry import ConnectorRegistry
from trove.services.kb.service import KbService, TableNotes, TermHit
from trove.core.logging import get_logger
from trove.workflow.state import WorkflowState

logger = get_logger(__name__)

# 零匹配兜底的全量表数量上限(金融 dev 集 8 张表;防超长 context)
FALLBACK_TABLES_LIMIT = 8

_QUOTED_RE = re.compile(r"['\"]([^'\"]{2,30})['\"]")
_CAPITALIZED_RE = re.compile(r"\b[A-Z][a-zA-Z]{2,}\b")
_ALL_CAPS_RE = re.compile(r"\b[A-Z]{2,}\b")


def _dedup_tables(hits: list[TermHit]) -> list[str]:
    """Flatten term table lists, preserving order and dropping duplicates."""
    names: list[str] = []
    for hit in hits:
        for table in hit.tables:
            if table not in names:
                names.append(table)
    return names


def _column_line(col: dict[str, Any], notes: TableNotes | None) -> str:
    desc = notes.columns.get(col["name"]) if notes else None
    enums = notes.enums.get(col["name"]) if notes else ""
    base = f"{col['name']} ({col['type']})"
    if enums:
        base += f" — values: {enums}"
    return f"{base} — {desc}" if desc else base


def _extract_value_candidates(text: str, limit: int = 8) -> list[str]:
    """Value-linking candidates from question + evidence.

    Extracts quoted strings, adjacent all-caps word phrases (merged into
    one candidate: 'POPLATEK TYDNE', not 'POPLATEK' + 'TYDNE' — the DB
    stores the full phrase), and capitalized words (e.g. 'Benesov').
    Plain lowercase/Chinese questions yield nothing.
    """
    candidates: list[str] = []
    for m in _QUOTED_RE.finditer(text):
        candidates.append(m.group(1))
    caps = list(_ALL_CAPS_RE.finditer(text))
    i = 0
    while i < len(caps):
        m = caps[i]
        phrase = m.group(0)
        j = i + 1
        # 相邻(仅隔空白)的全大写词合并为短语
        while (
            j < len(caps)
            and caps[j - 1].end() < caps[j].start()
            and not text[caps[j - 1].end(): caps[j].start()].strip()
        ):
            phrase += " " + caps[j].group(0)
            j += 1
        candidates.append(phrase)
        i = j
    for m in _CAPITALIZED_RE.finditer(text):
        candidates.append(m.group(0))

    seen: set[str] = set()
    result = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            result.append(c)
    return result[:limit]


async def _find_value_hits(
    connectors: ConnectorRegistry, details: list[dict[str, Any]],
    candidates: list[str],
) -> dict[str, str]:
    """Which candidates appear as actual values in the tables' text columns.

    Searches ALL given tables (not just the already-matched ones), so a
    literal value mentioned in the question/evidence can pull its table
    into the match set (e.g. 'POPLATEK TYDNE' → account.frequency).

    Returns:
        Map of value → location ("table.column").
    """
    try:
        adapter = await connectors.get()
        quote = "`" if adapter.dialect() == "mysql" else '"'
    except Exception:
        return {}

    hits: dict[str, str] = {}
    for detail in details:
        text_cols = [
            c["name"] for c in detail["columns"]
            if any(t in str(c["type"]).lower() for t in ("char", "text"))
        ][:3]
        for col in text_cols:
            for value in candidates:
                if value in hits:
                    continue
                escaped = value.replace("'", "''")
                sql = (
                    f"SELECT 1 FROM {quote}{detail['name']}{quote} "
                    f"WHERE {quote}{col}{quote} = '{escaped}' LIMIT 1"
                )
                try:
                    result = await asyncio.wait_for(
                        connectors.execute(sql), timeout=5.0,
                    )
                except Exception:
                    continue
                if result.row_count > 0:
                    hits[value] = f"{detail['name']}.{col}"
    return hits


def _fk_neighbors(
    seeds: list[str], all_columns: dict[str, list[str]],
) -> tuple[list[str], list[str]]:
    """FK 一跳邻域扩展:正向 seed.*_id → 目标表;反向其他表.*_id → seed。

    BIRD 题常只点名一个实体("clients"),联表所需的关联表(disp)靠
    FK 命名约定带出。不含递归——只扩一跳,预算封顶在调用侧。
    """
    seed_set = set(seeds)
    forward: list[str] = []
    for table in seeds:
        for col in all_columns.get(table, []):
            if col.endswith("_id") and len(col) > 3:
                target = col[:-3]  # district_id → district
                if (
                    target != table
                    and target in all_columns
                    and target not in seed_set
                    and target not in forward
                ):
                    forward.append(target)
    reverse: list[str] = []
    for tname, cols in all_columns.items():
        if tname in seed_set:
            continue
        if any(c.endswith("_id") and c[:-3] in seed_set for c in cols):
            reverse.append(tname)
    return forward, reverse


def _join_hints(
    table_name: str, columns: list[str], table_columns: dict[str, list[str]],
) -> list[str]:
    """Infer join paths from *_id column names (works without FK metadata).

    e.g. account.district_id with a district table present →
    "account.district_id → district.district_id" (falls back to ".id").

    Args:
        table_name: The table whose columns are being inspected.
        columns: That table's column names.
        table_columns: Map of candidate target table → its column names.

    Returns:
        Join hint strings ("<table>.<col> → <target>.<target_col>").
    """
    hints = []
    for col in columns:
        if not col.endswith("_id") or len(col) <= 3:
            continue
        target = col[:-3]  # district_id → district
        if target == table_name or target not in table_columns:
            continue
        target_cols = table_columns[target]
        target_col = col if col in target_cols else ("id" if "id" in target_cols else None)
        if target_col:
            hints.append(f"{table_name}.{col} → {target}.{target_col}")
    return hints


def make_schema_linking(
    catalog: CatalogService | None = None,
    max_tables: int = 5,
    kb: KbService | None = None,
    connectors: ConnectorRegistry | None = None,
    fallback_all: bool = True,
) -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
    """Build the schema_linking node bound to catalog and knowledge base.

    Args:
        catalog: Metadata catalog for table search (None → pass through empty).
        max_tables: Maximum tables to match per query.
        kb: Optional knowledge base for term matching and annotations.
        connectors: Registry providing the active datasource name (KB scope).
            KB is only consulted when a datasource context exists.
        fallback_all: When nothing matched, fall back to the full table list
            (bounded) so generation stays anchored to the real schema.
            Disable in clarify mode, where zero matches should ask the user.

    Returns:
        Async node function taking WorkflowState and returning a partial update.
    """

    async def schema_linking(state: WorkflowState) -> dict[str, Any]:
        # Upstream node failed — pass through without running
        if state.error:
            return {}

        # Knowledge base is scoped to the active datasource
        datasource = connectors.default_name if connectors is not None else ""

        # 带上下文重跑：诊断文本参与检索，诊断中提到的表/术语可重新进入匹配
        search_query = (
            (state.question + "\n" + state.error_analysis).strip()
            if state.error_analysis else state.question
        )

        # 1. Knowledge base term matching (substring, works for Chinese)
        term_hits: list[TermHit] = []
        if kb is not None and datasource:
            await kb.ensure_synced(default_datasource=datasource)
            term_hits = await kb.search_terms(search_query, datasource)

        update: dict[str, Any]

        if catalog is None:
            update = {
                "matched_tables": _dedup_tables(term_hits),
                "schema_context": "No schema information available.",
            }
        else:
            # 2. Catalog table search (existing behavior)
            try:
                matches = await catalog.search_tables(search_query, limit=max_tables)
            except Exception as e:
                logger.error("Schema linking failed: %s", e)
                return {"error": f"Schema linking failed: {e}"}

            # 全表 detail 一次取齐:value 探测与 FK 邻域扩展都基于全表
            all_names: list[str] = []
            try:
                all_names = [
                    t["name"] for t in await catalog.list_tables(datasource or None)
                ]
            except Exception:
                pass
            all_details: list[dict[str, Any]] = []
            for name in all_names:
                try:
                    detail = await catalog.table_detail(name)
                except Exception:
                    continue
                if detail is not None:
                    all_details.append(detail)
            all_columns = {
                d["name"]: [c["name"] for c in d["columns"]] for d in all_details
            }

            # 匹配优先级:表名命中 > 字面值命中 > KB 术语 > FK 邻域(正向→反向)
            # > 列名命中(最弱)。列名命中放最后是因为它最常假阳性
            # ("issue"→card.issued),扩表时优先采纳强证据。
            name_matches = [m["name"] for m in matches if m.get("match_type") == "name"]
            col_matches = [m["name"] for m in matches if m.get("match_type") != "name"]
            matched_names = [t for t in name_matches]

            # Value linking(全表):question/evidence 中的字面值出现在某表
            # → 该表加入匹配集('POPLATEK TYDNE' → account.frequency)。
            value_tables: list[str] = []
            value_hits: dict[str, str] = {}
            if connectors is not None:
                candidates = _extract_value_candidates(
                    (state.question + "\n" + state.evidence).strip()
                )
                if candidates:
                    value_hits = await _find_value_hits(
                        connectors, all_details, candidates,
                    )
                    for loc in value_hits.values():
                        t = loc.split(".", 1)[0]
                        if t not in matched_names and t not in value_tables:
                            value_tables.append(t)
            matched_names += value_tables

            # KB 术语绑定表(人工语义知识,强证据)
            for table in _dedup_tables(term_hits):
                if table not in matched_names:
                    matched_names.append(table)

            # FK 一跳邻域:正向(matched.*_id → 目标表)再反向(其他表.*_id
            # → matched),把联表所需但问题里没点名的表带进来
            # (如 "clients" 需要 disp 做 client–account 关联)。
            fwd, rev = _fk_neighbors(matched_names, all_columns)
            for t in fwd + rev:
                if t not in matched_names:
                    matched_names.append(t)

            # 列名命中垫底:只填空位,不挤掉强证据表
            for t in col_matches:
                if t not in matched_names:
                    matched_names.append(t)

            # 回退重跑:上一轮已匹配的表保留(并集),修"漏表"不丢旧匹配
            if state.error_feedback or state.error_analysis or state.retry_count:
                for table in state.matched_tables:
                    if table not in matched_names:
                        matched_names.append(table)

            # 预算封顶:优先级已在顺序中体现,超出截断
            matched_names = matched_names[:FALLBACK_TABLES_LIMIT]

            # 兜底:分词/复数/缩写匹配不到任何表时(典型英文 BIRD 题),
            # 退回全量表清单,保证生成锚定在真实 schema 上。
            # clarify 模式不需要兜底——0 匹配应当触发反问用户。
            if not matched_names and fallback_all:
                matched_names = all_names[:FALLBACK_TABLES_LIMIT]

            # 3. Human annotations merged into the schema context
            notes = (
                await kb.table_notes(matched_names, datasource)
                if (kb is not None and datasource) else {}
            )
            details_by_name = {d["name"]: d for d in all_details}
            details = [
                details_by_name[name] for name in matched_names
                if name in details_by_name
            ]
            table_columns = {d["name"]: [c["name"] for c in d["columns"]] for d in details}

            schema_parts = []
            for detail in details:
                name = detail["name"]
                table_notes = notes.get(name)
                cols = ", ".join(
                    _column_line(c, table_notes) for c in detail["columns"]
                )
                parts = [
                    f"Table: {name}\n"
                    f"Columns: {cols}\n"
                    f"Approximate rows: {detail.get('row_count', 'unknown')}\n",
                ]
                hints = _join_hints(
                    name, [c["name"] for c in detail["columns"]], table_columns,
                )
                if hints:
                    parts.append("Join hints: " + ", ".join(hints) + "\n")
                if table_notes and table_notes.description:
                    parts.append(f"Description: {table_notes.description}\n")
                if table_notes and table_notes.metrics:
                    parts.append(
                        "Metrics:\n"
                        + "".join(f"- {m} — {d}\n" for m, d in table_notes.metrics.items())
                    )
                schema_parts.append("".join(parts))

            schema_context = "\n".join(schema_parts) if schema_parts else (
                "No matching tables found. Consider using /tables to list available tables."
            )

            # 4. Value linking hints: show hits whose table made it into context
            if value_hits:
                shown = {
                    v: loc for v, loc in value_hits.items()
                    if loc.split(".", 1)[0] in matched_names
                }
                if shown:
                    hint_lines = "\n".join(
                        f"Value hints: '{v}' found in {loc}"
                        for v, loc in shown.items()
                    )
                    schema_context += "\n\n" + hint_lines

            logger.debug(
                "Schema linking matched %d tables for query: %s",
                len(matched_names), state.question[:80],
            )

            update = {
                "matched_tables": matched_names,
                "schema_context": schema_context,
            }

        if term_hits:
            update["kb_hits"] = [
                {
                    "kind": "term",
                    "term": h.term,
                    "mapping": h.mapping,
                    "definition": h.definition,
                    "tables": h.tables,
                }
                for h in term_hits
            ]
        return update

    return schema_linking
