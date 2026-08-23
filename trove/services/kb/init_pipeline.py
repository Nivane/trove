"""LLM-assisted KB initialization pipeline (shared by REPL /kb init and
the admin API).

Migrated verbatim from the CLI command (trove/cli/commands/kb_cmds.py):
descriptions are drafted by the LLM in bounded table chunks, then merged
with probing/profiling evidence; terms/examples are generated
deterministically. No-LLM → plain schema skeleton; refusing to overwrite
an initialized datasource unless overwrite=True.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from trove.core.errors import DatasourceError
from trove.core.logging import get_logger
from trove.prompts import render
from trove.services.kb.deterministic_gen import generate_terms, generate_templates
from trove.services.kb.docs_import import apply_docs, load_docs_tables
from trove.services.kb.enum_probe import merge_into_notes, probe_enums
from trove.services.kb.profiling import merge_into_stats, probe_stats
from trove.services.kb.semantic_draft import draft_semantic_annotations
from trove.services.kb.semantic_gen import generate_semantic_document
from trove.services.kb.synthetic import (
    generate_synthetic_examples,
    schema_text,
    validate_examples,
)

logger = get_logger(__name__)

# /kb init chunking: LLM output is capped, so large schemas are drafted
# in batches of tables and merged afterwards.
# 5 表/块:推理模型 CoT 波动会挤占 max_tokens 预算、正文在中间被截断
# (deepseek-reasoner 实测:8 表单块在 207 行处被切,修复轮第 6 行被切);
# 单块减半输出,给 CoT 波动留足缓冲。
INIT_CHUNK_TABLES = 5
# 推理模型(deepseek-reasoner 实测)的 max_tokens 计入 CoT:8192 时思考即
# 耗尽全部预算、content 为空(finish_reason=length);16384 给 CoT+草稿
# 正文留足余量(实测 8 表带统计提示词:CoT ~6.3k + 正文 ~2.1k tokens)。
INIT_MAX_TOKENS = 16384


def _strip_fences(text: str) -> str:
    """Strip markdown code fences (```yaml ... ```) if present."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        lines = lines[1:]  # drop the opening fence (``` or ```yaml)
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines)
    return stripped


def _parse_draft(response: str) -> dict:
    """Parse the LLM draft into {example: dict, terms: list[dict]}.

    Raises:
        ValueError: If the response is not a usable draft.
    """
    data = yaml.safe_load(_strip_fences(response)) or {}
    if not isinstance(data, dict) or not isinstance(data.get("example"), dict):
        raise ValueError("draft missing 'example' section")
    example = data["example"]
    if not example.get("question") or not example.get("sql"):
        raise ValueError("example missing question/sql")
    return {"example": example, "terms": list(data.get("terms") or [])}


def _fenced_blocks(text: str) -> list[str]:
    """Fenced code blocks (``` … ``` / ```yaml … ```) anywhere in the text,
    last first — reasoning 文本里越靠后的围栏越接近最终草稿。"""
    blocks = re.findall(r"```[a-zA-Z]*\s*\n(.*?)```", text, re.DOTALL)
    return list(reversed(blocks))


def _recover_draft(response: str) -> dict | None:
    """Salvage a learn draft (example/terms) from prose via fenced blocks."""
    for candidate in _fenced_blocks(response):
        try:
            return _parse_draft(candidate)
        except Exception:
            continue
    return None


def _table_dicts(chunk, docs: dict) -> list[dict]:
    """SchemaInfo 表块 → 字典列表,官方文档描述预填(docs 权威)。"""
    tables = []
    for t in chunk:
        doc = docs.get(t.name, {})
        columns = []
        for c in t.columns:
            entry = doc.get(c.name, {})
            columns.append({
                "name": c.name,
                "type": c.type,
                "description": str(entry.get("description", "") or ""),
                "enums": list(entry.get("enums") or []),
            })
        tables.append({"name": t.name, "description": "", "columns": columns, "metrics": []})
    return tables


def _chunk_tables(schema, size: int | None = None):
    """Yield schema tables in chunks (large schemas exceed one LLM output)."""
    if size is None:
        size = INIT_CHUNK_TABLES  # read at call time (tests monkeypatch it)
    tables = schema.tables
    for i in range(0, len(tables), size):
        yield tables[i : i + size]


def _parse_init_tables(response: str) -> list:
    """Parse the init draft into table annotations (LLM 只起草描述).

    Accepts both shapes LLMs actually produce:
      - ONE document with a `tables` section (extra top-level keys
        tolerated — 旧版三顶层键格式的 terms/examples 被忽略)
      - multiple '---'-separated documents, the one carrying `tables` wins

    Raises:
        ValueError: Missing/empty tables section.
    """
    docs = [d for d in yaml.safe_load_all(_strip_fences(response)) if d]

    tables = None
    for doc in docs:
        if isinstance(doc, dict) and isinstance(doc.get("tables"), list):
            tables = doc["tables"]
            break

    if not tables:
        raise ValueError(f"draft must contain a 'tables' section, got {len(docs)} doc(s)")
    # LLM 只起草描述:stats/row_count 是 profiling 的确定性产物,LLM 输出
    # 里的同名键一律剥掉,防止模型"写统计"污染证据源。
    for table in tables:
        table.pop("row_count", None)
        for col in table.get("columns", []):
            col.pop("stats", None)
    return tables


def _recover_init_tables(response: str) -> list | None:
    """Salvage a `tables` draft from a response that is thinking prose
    (推理模型 content 为空时网关回退 reasoning),而非干净 YAML。

    候选依次为:文本内 ```yaml 围栏块(从后往前)、最靠后的顶格
    `tables:` 起的尾部片段;任一可解析即采用,全部失败返回 None。
    """
    candidates = _fenced_blocks(response)
    tail_starts = [m.start() for m in re.finditer(r"(?m)^tables:\s*$", response)]
    if tail_starts:
        candidates.append(response[tail_starts[-1]:])
    for candidate in candidates:
        try:
            return _parse_init_tables(candidate)
        except Exception:
            continue
    return None


async def _draft_init_chunk(
    llm, model, tables, samples=None, stats=None, lang: str = "en",
) -> list:
    """Draft one schema chunk; on parse failure, one LLM repair round.

    Raises:
        Exception: Parse failure even after the repair round.
    """
    messages = [
        {"role": "system", "content": render("kb/init_system", lang=lang)},
        {"role": "user", "content": render(
            "kb/init_user", schema_text=schema_text(tables, samples, stats))},
    ]
    expected = {t["name"] for t in tables}

    def parse_try(response: str) -> tuple[list | None, list[str], str | None]:
        """→ (tables, missing, parse_error)。正规解析失败 → 从 prose 回收
        (推理模型空 content 回退);草稿必须覆盖块内全部表,缺表单独携带:
        防推理截断的残缺草稿静默入库,并给修复轮精确的缺表清单。"""
        try:
            draft = _parse_init_tables(response)
        except Exception as e:
            draft = _recover_init_tables(response)
            if draft is None:
                return None, [], str(e)
        missing = sorted(expected - {t["name"] for t in draft})
        return draft, missing, None

    draft, missing, parse_error = parse_try(await llm.chat(
        model=model, messages=messages, max_tokens=INIT_MAX_TOKENS,
    ))
    if not missing and parse_error is None:
        return draft

    first_error = parse_error or f"draft missing table(s): {', '.join(missing)}"
    logger.warning("Could not parse init draft: %s; asking for repair", first_error)

    if draft is not None:
        # 部分草稿 → 外科手术式修复:只补缺表。任务变小后推理 CoT 更短,
        # 在 max_tokens 预算内更容易产出完整正文;补完按表名合并。
        repair_response = await llm.chat(
            model=model,
            max_tokens=INIT_MAX_TOKENS,
            messages=[*messages, {"role": "user", "content": render(
                "kb/init_repair_missing", missing=", ".join(missing))}],
        )
        try:
            repaired = _parse_init_tables(repair_response)
        except Exception:
            repaired = _recover_init_tables(repair_response)
        if repaired:
            known = {t["name"] for t in draft}
            for t in repaired:
                if t["name"] in expected and t["name"] not in known:
                    draft.append(t)
                    known.add(t["name"])
        missing = sorted(expected - known)
        if missing:
            raise ValueError(f"draft missing table(s): {', '.join(missing)}")
        # 只保留块内表:草稿/修复里的多余表一并剥掉(旧整块修复同样丢弃
        # 残缺草稿,不把噪声带进 schema_notes)。
        return [t for t in draft if t["name"] in expected]

    repair_response = await llm.chat(
        model=model,
        max_tokens=INIT_MAX_TOKENS,
        messages=[*messages, {"role": "user", "content": render(
            "kb/init_repair", error=first_error)}],
    )
    draft, missing, repair_error = parse_try(repair_response)
    if draft is None or missing:
        raise ValueError(repair_error or f"draft missing table(s): {', '.join(missing)}")
    return draft


async def _append_synthetic_examples(
    examples: list[dict],
    llm,
    model: str,
    all_tables: list[dict],
    registry_svc,
    datasource: str,
    *,
    samples: dict,
    stats: dict,
    lang: str,
) -> list[dict]:
    """合成 few-shot(SQL-to-Text):LLM 基于带统计的 schema 生成 Q/SQL 对。

    逐块生成(与起草同为 INIT_CHUNK_TABLES),每块经 SQLGlot 语法 +
    试执行(LIMIT 1)双护栏;任何失败(解析失败/执行失败/超时)整批
    静默跳过——确定性模板仍是兜底,合成 few-shot 只是锦上添花。
    """
    try:
        synthetic: list[dict] = []
        for i in range(0, len(all_tables), INIT_CHUNK_TABLES):
            chunk = all_tables[i : i + INIT_CHUNK_TABLES]
            generated = await generate_synthetic_examples(
                llm, model, chunk, samples=samples, stats=stats, lang=lang,
            )
            synthetic.extend(await validate_examples(
                generated, registry_svc, datasource or None,
            ))
        return examples + synthetic
    except Exception as e:
        logger.warning("Synthetic few-shot generation skipped: %s", e)
        return examples


def _backfill_types(tables: list[dict], schema) -> None:
    """LLM 草稿可能漏写 type:从真实 schema 回填(确定性生成依赖列类型)。"""
    schema_types = {
        t.name: {c.name: c.type for c in t.columns} for t in schema.tables
    }
    for table in tables:
        for col in table.get("columns", []):
            if not col.get("type"):
                col["type"] = schema_types.get(table.get("name", ""), {}).get(
                    col.get("name", ""), "",
                )


def _backfill_pks(tables: list[dict], schema) -> None:
    """回填主键标记(COUNT 术语的 id 列选取优先用 primary_key)。

    只回填草稿里存在的列;被 LLM 草稿丢弃的列回退到命名规则
    (_is_id_column),确定性不依赖列数。
    """
    schema_pks = {
        t.name: {c.name: c.primary_key for c in t.columns} for t in schema.tables
    }
    for table in tables:
        for col in table.get("columns", []):
            if "primary_key" not in col:
                col["primary_key"] = schema_pks.get(
                    table.get("name", ""), {},
                ).get(col.get("name", ""), False)


async def init_kb(kb, registry, llm, config, datasource, *,
                  overwrite: bool = False, docs: str = "", lang: str = "en") -> str:
    """LLM-assisted KB initialization (shared by REPL /kb init and the
    admin API). No-LLM → plain schema skeleton. Refuses to overwrite an
    initialized datasource unless overwrite=True."""
    # 显式传 datasource:admin 可初始化非默认源,无参会解析到默认源
    # (多源必触发——新注册源即默认的 T3 minor 下错写更隐蔽)
    schema = await registry.get_schema(datasource)
    if llm is None:
        if kb.init_schema_notes(schema, datasource, overwrite=overwrite):
            return (f"Created .trove/kb/{datasource}/schema_notes.yml skeleton. "
                    f"Fill in table/column descriptions, then /kb reload.")
        raise DatasourceError(
            f".trove/kb/{datasource}/schema_notes.yml already exists — refusing to overwrite.",
            datasource=datasource,
        )
    existing = kb.init_exists(datasource)
    if existing and not overwrite:
        raise DatasourceError(
            f".trove/kb/{datasource}/ already has {', '.join(existing)} — "
            f"refusing to overwrite. Pass overwrite=true to re-initialize.",
            datasource=datasource,
        )
    model = (config.target if config else "") or "openai/gpt-4o"
    docs_tables = load_docs_tables(Path(docs)) if docs else {}
    probed: dict = {}
    try:
        probed = await probe_enums(registry, schema)
    except Exception:
        pass
    profiled: dict = {}
    try:
        profiled = await probe_stats(registry, schema)
    except Exception:
        pass
    all_tables: list[dict] = []
    for chunk in _chunk_tables(schema):
        try:
            tables = await _draft_init_chunk(
                llm, model, _table_dicts(chunk, docs_tables),
                samples=probed, stats=profiled, lang=lang,
            )
        except Exception as e:
            logger.warning("Could not parse init draft after repair: %s", e)
            raise DatasourceError(
                f"Could not parse LLM draft after repair: {e}", datasource=datasource,
            ) from e
        all_tables.extend(tables)
    _backfill_types(all_tables, schema)
    _backfill_pks(all_tables, schema)
    all_tables = apply_docs(all_tables, docs_tables)
    if profiled:
        all_tables = merge_into_stats({"tables": all_tables}, profiled)["tables"]
    if probed:
        all_tables = merge_into_notes({"tables": all_tables}, probed)["tables"]
    terms = generate_terms(all_tables, lang=lang)
    examples = generate_templates(all_tables, lang=lang)
    examples = await _append_synthetic_examples(
        examples, llm, model, all_tables, registry, datasource,
        samples=probed, stats=profiled, lang=lang,
    )
    kb.init_notes(all_tables, datasource, overwrite=overwrite)
    semantic_doc = generate_semantic_document(schema, model_name=datasource, terms=terms)
    # P4:有 LLM 时在结构层之上起草字段 synonyms/description(白名单,只加措辞
    # 不改结构);任何失败静默回退纯结构层。
    try:
        semantic_doc = await draft_semantic_annotations(
            llm, model, semantic_doc, lang=lang)
    except Exception as e:
        logger.warning("Semantic draft skipped: %s", e)
    kb.init_semantics(semantic_doc, datasource, overwrite=overwrite)
    kb.init_examples(examples, datasource, overwrite=overwrite)
    await kb.force_sync(datasource)
    return (f"Initialized .trove/kb/{datasource}/: {len(all_tables)} tables annotated, "
            f"{len(terms)} terms, {len(examples)} templates. "
            f"Review the drafts, then /kb reload.")
