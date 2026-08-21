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
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from trove.prompts import render
from trove.prompts.skills import render_skills
from trove.services.datasource.catalog import CatalogService
from trove.services.datasource.registry import ConnectorRegistry
from trove.services.kb.service import KbService, TableNotes, TermHit
from trove.core.logging import get_logger
from trove.llm.observability import record_span
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


def _semantic_metric_line(m: Any) -> str:
    """语义层 metric 一行:名字:表达式 — 业务定义。"""
    base = f"- {m.name}: {m.expression}"
    return f"{base} — {m.definition}" if m.definition else base


_NOTABLE_SHAPES = {"all_caps", "capital", "text"}  # 常见形状不显示(与枚举含义重复)


def _stats_lines(stats: dict[str, dict[str, Any]]) -> list[str]:
    """仅渲染有信息量的统计行(null 比例高/形状异常/数值日期范围)。

    平凡统计(0% NULL、高 distinct)与常见形状不显示——上下文预算
    留给真正异常的证据(AskData:统计的价值在"异常"处)。
    """
    lines = []
    for col_name, st in stats.items():
        bits = []
        nr = st.get("null_ratio")
        if nr is not None and nr >= 0.3:
            bits.append(f"{round(nr * 100)}% NULL")
        shape = st.get("shape")
        if shape and shape not in _NOTABLE_SHAPES:
            bits.append(f"shape={shape}")
        if st.get("min") is not None and st.get("max") is not None:
            bits.append(f"range {st['min']} .. {st['max']}")
        if bits:
            lines.append(f"- {col_name}: " + ", ".join(bits))
        # Top-K 值(低基数文本列):规范拼写/脏值就藏在这里,直接可见
        tv = st.get("top_values")
        if tv:
            shown = ", ".join(f"{v} ({c})" for v, c in tv[:5])
            lines.append(f"- {col_name}: top values: {shown}")
    return lines


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


_HINT_RE = re.compile(r"^(\S+)\.(\S+) → (\S+)\.(\S+)$")
JOIN_PROBE_SAMPLE = 10   # 每个 hint 的采样值数
JOIN_MIN_MATCH = 2       # 命中数下限(命名约定可能是错的,数据证据兜底)


def _hint_tables(hint: str) -> tuple[str, str] | None:
    """hint 字符串 → (源表, 目标表);无法解析返回 None。"""
    m = _HINT_RE.match(hint)
    return (m.group(1), m.group(3)) if m else None


async def _verified_hints(
    connectors: ConnectorRegistry | None, hints: list[str],
) -> dict[str, str]:
    """数据级验证 join hints(采样值重叠探测,零 LLM)。

    对每个 hint(a.x → b.y):采样 a.x 前 10 个非 NULL 值,统计 b.y 命中数。
    命中 ≥2 才发布;部分命中附重叠比率("a.x → b.y (7/10 match)")。
    探测失败/超时 → 丢弃该 hint(静默,护栏同 value 探测)。
    connectors 不可用 → 原样返回(无法验证不阻塞)。

    Returns:
        原始 hint → 发布文本(可能带重叠后缀;未通过验证的 hint 不在映射里)。
    """
    if connectors is None or not hints:
        return {h: h for h in hints}
    try:
        adapter = await connectors.get()
        quote = "`" if adapter.dialect() == "mysql" else '"'
    except Exception:
        return {h: h for h in hints}

    async def verify(hint: str) -> tuple[str, str] | None:
        m = _HINT_RE.match(hint)
        if not m:
            return (hint, hint)
        src_t, src_c, dst_t, dst_c = m.groups()
        sample_sql = (
            f"SELECT {quote}{src_c}{quote} FROM {quote}{src_t}{quote} "
            f"WHERE {quote}{src_c}{quote} IS NOT NULL LIMIT {JOIN_PROBE_SAMPLE}"
        )
        try:
            res = await asyncio.wait_for(connectors.execute(sample_sql), timeout=5.0)
        except Exception:
            return None
        values = [r[0] for r in (res.rows or []) if r and r[0] is not None]
        if not values:
            return None
        in_list = ", ".join(f"'{str(v).replace(chr(39), chr(39) * 2)}'" for v in values)
        count_sql = (
            f"SELECT COUNT(*) FROM {quote}{dst_t}{quote} "
            f"WHERE {quote}{dst_c}{quote} IN ({in_list})"
        )
        try:
            res = await asyncio.wait_for(connectors.execute(count_sql), timeout=5.0)
        except Exception:
            return None
        hits = int(res.rows[0][0]) if res.rows and res.rows[0] else 0
        # 命中 ≥2 才发布;小表(采样不足 2 行)时全部命中视为通过。
        if hits < JOIN_MIN_MATCH and hits < len(values):
            return None
        published = f"{hint} ({hits}/{len(values)} match)" if hits < len(values) else hint
        return (hint, published)

    results = await asyncio.gather(*[verify(h) for h in hints])
    mapping: dict[str, str] = {}
    for item in results:
        if item is None:
            continue
        orig, published = item
        mapping[orig] = published
    return mapping


# ── LLM 对齐裁剪(AskData Task Alignment)──────────────────


def _alignment_context(
    details: list[dict[str, Any]], notes: dict[str, TableNotes] | None,
    hints_by_table: dict[str, list[str]] | None = None,
) -> str:
    """候选表的紧凑对齐上下文:行数 + join hints + 每列类型与统计证据。

    无 stats 的表退化为 "col (type)" 清单——对齐仍可基于列名与类型判断;
    统计存在时给出 null 比例/基数/形状/范围,供 LLM 判断列的可用性。
    Top-K 值(低基数列的规范拼写)与已数据级验证的 join hints 也进上下文:
    前者让对齐看到列的实际内容,后者提醒它别裁掉连接路径两端的表。
    """
    lines = []
    for detail in details:
        name = detail["name"]
        table_notes = notes.get(name) if notes else None
        stats = (table_notes.stats if table_notes else {}) or {}
        row_count = (
            table_notes.row_count
            if (table_notes and table_notes.row_count is not None)
            else detail.get("row_count")
        )
        header = f"Table: {name}"
        if row_count:
            header += f" ({row_count} rows)"
        lines.append(header)
        hints = (hints_by_table or {}).get(name) or []
        if hints:
            lines.append("Join hints: " + ", ".join(hints))
        for col in detail["columns"]:
            cname = col["name"]
            bits = []
            st = stats.get(cname) or {}
            nr = st.get("null_ratio")
            if nr is not None and nr >= 0.1:
                bits.append(f"{round(nr * 100)}% NULL")
            if st.get("distinct") is not None:
                bits.append(f"{st['distinct']} distinct")
            shape = st.get("shape")
            if shape and shape != "text":
                bits.append(shape)
            if st.get("min") is not None and st.get("max") is not None:
                bits.append(f"{st['min']}..{st['max']}")
            line = f"  {cname} ({col['type']})"
            if bits:
                line += ": " + ", ".join(bits)
            lines.append(line)
            tv = st.get("top_values")
            if tv:
                shown = ", ".join(f"{v} ({c})" for v, c in tv[:5])
                lines.append(f"    top values: {shown}")
    return "\n".join(lines)


def _parse_alignment(response: str) -> dict[str, Any] | None:
    """严格 JSON 解析(容忍 markdown 围栏);格式错误 → None(回退)。"""
    text = (response or "").strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if m:
        text = m.group(1)
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    keep = data.get("keep_tables")
    drop = data.get("drop_columns") or {}
    if not isinstance(keep, list) or not isinstance(drop, dict):
        return None
    return {
        "keep_tables": [str(t) for t in keep],
        "drop_columns": {
            str(k): [str(c) for c in v]
            for k, v in drop.items() if isinstance(v, list)
        },
    }


def _apply_alignment(
    matched: list[str],
    alignment: dict[str, Any] | None,
    all_columns: dict[str, set[str]],
    must_keep: list[str] | None = None,
) -> tuple[list[str], dict[str, set[str]]]:
    """对齐结果应用到匹配集:保序过滤 + 列裁剪(校验到真实列)。

    空 keep 结果(LLM 全删)视为失败 → 原样返回;must_keep(回退重跑
    的上一轮匹配表)强制保留——对齐不允许丢掉诊断已经钦点的表。
    """
    if not alignment:
        return matched, {}
    must = {t for t in (must_keep or []) if t in matched}
    keep = [t for t in matched if t in must or t in alignment["keep_tables"]]
    if not keep:
        return matched, {}
    drop: dict[str, set[str]] = {}
    for table, cols in alignment["drop_columns"].items():
        if table in keep:
            drop[table] = {c for c in cols if c in all_columns.get(table, set())}
    return keep, drop


def _alignment_system_prompt(lang: str) -> str:
    """对齐 system prompt:base 模板 + 方法论 skill(manifest 触发,节点级)。"""
    system_prompt = render("schema_alignment/system", lang=lang)
    skill_block = render_skills("schema_linking", lang=lang)
    if skill_block:
        system_prompt = f"{system_prompt}\n\n{skill_block}"
    return system_prompt


async def _align_tables(
    llm: Any, config: Any, state: WorkflowState,
    details: list[dict[str, Any]], notes: dict[str, TableNotes] | None,
    hints_by_table: dict[str, list[str]] | None = None,
) -> dict[str, Any] | None:
    """LLM 对齐调用:问题 + 候选表统计摘要 → {keep_tables, drop_columns}。

    失败/超时/格式错误 → None,管线原样回退(对齐不阻塞生成)。
    """
    if not details:
        return None
    # 表对齐判定是判别任务,走 fast 档(未配置 fast → 回退 target)
    model = ((config.model_fast or config.target) if config else "") or "openai/gpt-4o"
    try:
        response = await llm.chat(
            model=model,
            messages=[
                {"role": "system", "content": _alignment_system_prompt(state.lang)},
                {"role": "user", "content": render(
                    "schema_alignment/user",
                    lang=state.lang,
                    question=state.question,
                    evidence=state.evidence,
                    alignment_context=_alignment_context(
                        details, notes, hints_by_table),
                )},
            ],
            max_tokens=16000,
            metadata={
                "node": "schema_linking",
                "session_id": state.session_id,
                "run_id": state.run_id,
            },
        )
    except Exception as e:
        logger.debug("Alignment LLM call failed (proceeding without): %s", e)
        return None
    return _parse_alignment(response)


def make_schema_linking(
    catalog: CatalogService | None = None,
    max_tables: int = 5,
    kb: KbService | None = None,
    connectors: ConnectorRegistry | None = None,
    fallback_all: bool = True,
    llm: Any | None = None,
    config: Any | None = None,
    semantic_layer: Any | None = None,
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
        llm/config: Optional — when present, an LLM alignment step (AskData
            Task Alignment) trims the candidate tables/columns by question +
            statistics before the schema context is rendered.
        semantic_layer: Optional live semantic provider (OSSIE etc.) —
            metrics render into each matched table's section (datasets
            anchored) and a model-level block; never raises.

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
            # Oracle 锚(eval 专用):gold 表强制进匹配集首位,随后的 value/
            # term/FK/列名信号照常补位。oracle_tables 为空 → 完全等于既有
            # "name 命中优先"行为,生产路径零变化。
            oracle_kept = [t for t in state.oracle_tables if t in all_columns]
            matched_names = list(oracle_kept)
            for t in name_matches:
                if t not in matched_names:
                    matched_names.append(t)

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

            # 入口已带匹配表时并入(并集):回退重跑保留上轮匹配修"漏表",
            # 多步子任务则继承前序步骤的锚点(coordinator 预注入);全新
            # 单问题入口为空,行为不变。
            if state.matched_tables:
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

            # 3.1 实时语义层(OSSIE provider):外部语义文件查询时直读。
            # metric 表达式渲染进锚定表的 Semantic metrics 段;无数据集
            # 锚定的 metric 和模型级说明走模型级块。provider 自身保证
            # 软失败(last-known-good/逐条丢弃),这里再兜一层 try。
            live_metrics = []
            semantic_instructions = ""
            if (
                semantic_layer is not None and datasource
                and getattr(semantic_layer, "enabled", False)
            ):
                try:
                    live_metrics = list(semantic_layer.metrics() or [])
                    semantic_instructions = (
                        getattr(semantic_layer, "instructions", "") or "")
                except Exception as e:
                    logger.warning(
                        "Semantic layer lookup failed (%s): %s", datasource, e)
            details_by_name = {d["name"]: d for d in all_details}
            details = [
                details_by_name[name] for name in matched_names
                if name in details_by_name
            ]

            # 3.5 LLM 对齐裁剪(AskData Task Alignment):问题 + 统计摘要
            # → 保留表/裁剪列,防元数据膨胀后的上下文爆炸。回退重跑时
            # 上一轮匹配表强制保留(诊断钦点的表不允许被对齐丢弃)。
            # 无统计证据(未 kb init / 旧 KB)时跳过——没有 profiling 数据
            # 对齐就没有额外信号,不值得多一次 LLM 调用。
            table_columns = {
                d["name"]: [c["name"] for c in d["columns"]] for d in details
            }

            # P0-3:join hints 数据级验证——命名约定推断的 hint 用采样值
            # 重叠探测过滤,只把真实可连接的关联发布。先于对齐计算:
            # 已验证的连接路径同时进入对齐上下文(对齐因此能看出
            # 裁表会切断关联,不会把连接路径两端的表裁掉)。
            hints_by_table = {
                name: _join_hints(name, table_columns[name], table_columns)
                for name in table_columns
            }
            all_hints = [h for hs in hints_by_table.values() for h in hs]
            verified_map = await _verified_hints(connectors, all_hints)
            verified_hints_by_table = {
                t: [verified_map[h] for h in hs if h in verified_map]
                for t, hs in hints_by_table.items()
            }

            drop_columns: dict[str, set[str]] = {}
            has_stats_evidence = any(
                notes.get(d["name"]).stats if notes.get(d["name"]) else None
                for d in details
            )
            if llm is not None and details and has_stats_evidence:
                aligned = await _align_tables(
                    llm, config, state, details, notes, verified_hints_by_table)
                if aligned:
                    # must_keep:回退重跑时上一轮匹配表保留(修漏表不丢旧
                    # 匹配);oracle 表无条件保留(评测锚不允许被对齐裁掉)。
                    must_keep = [t for t in state.oracle_tables if t in matched_names]
                    if state.error_feedback or state.error_analysis or state.retry_count:
                        for t in state.matched_tables:
                            if t not in must_keep:
                                must_keep.append(t)
                    must_keep = must_keep or None
                    all_cols = {
                        d["name"]: {c["name"] for c in d["columns"]} for d in details
                    }
                    aligned_names, drop_columns = _apply_alignment(
                        matched_names, aligned, all_cols, must_keep=must_keep,
                    )
                    if aligned_names != matched_names:
                        matched_names = aligned_names
                        details = [
                            details_by_name[name] for name in matched_names
                            if name in details_by_name
                        ]
                    # 对齐裁掉了某些表后,连接路径两端的表必须都在保留集里,
                    # 否则 hint 引用了已裁表,发布给 gen_sql 会误导
                    kept = {d["name"] for d in details}
                    verified_hints_by_table = {
                        t: [
                            h for h in hs
                            if (ends := _hint_tables(h))
                            and ends[0] in kept and ends[1] in kept
                        ]
                        for t, hs in verified_hints_by_table.items()
                    }

            schema_parts = []
            for detail in details:
                name = detail["name"]
                table_notes = notes.get(name)
                cols = ", ".join(
                    _column_line(c, table_notes)
                    for c in detail["columns"]
                    if c["name"] not in drop_columns.get(name, set())
                )
                parts = [
                    f"Table: {name}\n"
                    f"Columns: {cols}\n"
                    f"Approximate rows: {detail.get('row_count', 'unknown')}\n",
                ]
                hints = verified_hints_by_table.get(name) or []
                if hints:
                    parts.append("Join hints: " + ", ".join(hints) + "\n")
                if table_notes and table_notes.description:
                    parts.append(f"Description: {table_notes.description}\n")
                if table_notes and table_notes.metrics:
                    parts.append(
                        "Metrics:\n"
                        + "".join(f"- {m} — {d}\n" for m, d in table_notes.metrics.items())
                    )
                anchored = [m for m in live_metrics if m.datasets and name in m.datasets]
                if anchored:
                    parts.append(
                        "Semantic metrics:\n"
                        + "".join(f"{_semantic_metric_line(m)}\n" for m in anchored)
                    )
                if table_notes and table_notes.stats:
                    stat_lines = _stats_lines(table_notes.stats)
                    if stat_lines:
                        parts.append("Stats:\n" + "".join(f"{s}\n" for s in stat_lines))
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

            # 4.5 模型级语义块:无数据集锚定的 metric + AI 使用说明
            # (锚定 metric 已进各表段;放最后以免挤占表段上下文)
            model_lines = []
            agnostic = [m for m in live_metrics if not m.datasets]
            if agnostic:
                model_lines.append(
                    "Semantic metrics:\n"
                    + "\n".join(_semantic_metric_line(m) for m in agnostic)
                )
            if semantic_instructions:
                model_lines.append(f"Semantic note: {semantic_instructions}")
            if model_lines:
                schema_context += "\n\n" + "\n\n".join(model_lines)

            logger.debug(
                "Schema linking matched %d tables for query: %s",
                len(matched_names), state.question[:80],
            )

            update = {
                "matched_tables": matched_names,
                "schema_context": schema_context,
            }

        if term_hits:
            hits = [
                {
                    "kind": "term",
                    "term": h.term,
                    "mapping": h.mapping,
                    "definition": h.definition,
                    "tables": h.tables,
                }
                for h in term_hits
            ]
            update["kb_hits"] = hits
            # KB 术语命中进 langfuse(无 Langfuse 时 no-op)
            with record_span(
                "kb.hits", input={"question": state.question},
            ) as span:
                if span is not None:
                    span.update(output={
                        "hits": [{"term": h["term"], "mapping": h["mapping"]}
                                 for h in hits],
                    })
        return update

    return schema_linking
