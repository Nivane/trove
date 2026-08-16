"""Knowledge base commands: /kb init | list | reload | learn [--yes].

/kb learn implements semi-automatic evolution: the LLM drafts an
example (+ candidate terms) from the last exchange, the draft is shown
and only written after the user confirms with /kb learn --yes.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from trove.cli.slash_registry import SlashRegistry, SlashCommand
from trove.core.logging import get_logger
from trove.services.kb.deterministic_gen import generate_terms, generate_templates
from trove.services.kb.docs_import import apply_docs, load_docs_tables
from trove.services.kb.enum_probe import merge_into_notes, probe_enums

logger = get_logger(__name__)

# /kb init chunking: LLM output is capped, so large schemas are drafted
# in batches of tables and merged afterwards.
INIT_CHUNK_TABLES = 10
INIT_MAX_TOKENS = 8192

DRAFT_SYSTEM_PROMPT = """You maintain a SQL knowledge base for a data agent. Given a user question and the SQL that answered it, draft a YAML document in strict YAML (no markdown fences, no commentary) with this shape:

example:
  question: <the question>
  sql: <the complete SQL on ONE single line>
  tags: [<2-4 short topic tags>]
terms:
  - term: <a business term appearing in the question>
    mapping: <SQL expression it corresponds to>
    tables: [<tables involved>]
    definition: <one-line definition>

IMPORTANT: every value must be on a single line with correct YAML indentation. Never continue a value on an unindented line."""

INIT_SYSTEM_PROMPT = """You are initializing a knowledge base for a SQL data agent. Given the schema below — column names, types, existing official descriptions, and sample values for low-cardinality text columns — produce ONE YAML document with a single top-level section `tables` (strict YAML, no markdown fences, no commentary, every value on a single line).

tables:
  - name: <table>
    description: <one-line business description in English>
    columns:
      - name: <column>
        description: <one-line description in English>
        enums: []
    metrics: []

Rules:
- For every column write a one-line English description (empty string "" if the column is truly opaque — do NOT guess the meaning of opaque columns like A1..A16 from name alone).
- Columns that already carry an official description must keep it unchanged; only fill the blanks.
- For text columns whose sample values are shown, fill enums with one "value=English meaning" entry per sample value, e.g. "POPLATEK MESICNE=monthly issuance".
- Business terms and reference templates are generated automatically by the system — do NOT add terms/examples sections."""

INIT_SYSTEM_PROMPT_ZH = """You are initializing a knowledge base for a SQL data agent. Given the schema below — column names, types, existing official descriptions, and sample values for low-cardinality text columns — produce ONE YAML document with a single top-level section `tables` (strict YAML, no markdown fences, no commentary, every value on a single line).

tables:
  - name: <table>
    description: <one-line business description in Chinese>
    columns:
      - name: <column>
        description: <one-line description in Chinese>
        enums: []
    metrics: []

Rules:
- For every column write a one-line Chinese description (empty string "" if the column is truly opaque — do NOT guess the meaning of opaque columns like A1..A16 from name alone).
- Columns that already carry an official description must keep it unchanged; only fill the blanks.
- For text columns whose sample values are shown, fill enums with one "value=中文含义" entry per sample value, e.g. "POPLATEK MESICNE=月发放 (monthly)".
- Business terms and reference templates are generated automatically by the system — do NOT add terms/examples sections."""


def _draft_prompt(question: str, sql: str) -> str:
    return f"Question: {question}\nSQL: {sql}\n"


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


def _schema_text(tables, samples: dict | None = None) -> str:
    """Compact schema listing for the init prompt.

    每列形如 "name type — 已有官方描述 [样例值; 样例值]":
    官方描述让 LLM 保留上下文,样例值帮 LLM 猜枚举含义。
    """
    samples = samples or {}
    lines = []
    for table in tables:
        cols = []
        for c in table["columns"]:
            line = f"{c['name']} {c['type']}"
            desc = str(c.get("description", "") or "").strip()
            if desc:
                line += f" — {desc}"
            values = samples.get(table["name"], {}).get(c["name"], "")
            if values:
                shown = "; ".join(values.split("; ")[:3])
                line += f" [{shown}]"
            cols.append(line)
        lines.append(f"{table['name']}: {', '.join(cols)}")
    return "\n".join(lines)


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
    return tables


def register_kb_commands(registry: SlashRegistry, context: dict) -> None:
    """Register the /kb command (subcommands: init, list, reload, learn).

    All operations are scoped to the active datasource
    (connector_registry.default_name): each datasource owns its own
    .trove/kb/<datasource>/ knowledge files.
    """

    def _datasource() -> str:
        registry_svc = context.get("connector_registry")
        if registry_svc is None:
            return ""
        return registry_svc.default_name or ""

    async def _cmd_learn(args: str) -> str:
        kb = context.get("kb")

        if args.strip().endswith("--yes"):
            draft = kb.pending_draft
            if not draft:
                return "No pending draft. Run /kb learn first."
            datasource = _datasource()
            if not datasource:
                return "No active datasource to write into. Connect a datasource first."
            kb.pending_draft = None
            await kb.append_example(draft["example"], datasource)
            for term in draft["terms"]:
                await kb.append_term(term, datasource)
            return (
                f"Learned: 1 example + {len(draft['terms'])} term(s) "
                f"written to .trove/kb/{datasource}/."
            )

        session = context.get("current_session")
        if not session:
            return "No active session. Ask a question first."

        question, sql = None, None
        for m in reversed(session.messages):
            if m.role == "assistant" and m.metadata.get("sql") and sql is None:
                sql = m.metadata["sql"]
            if m.role == "user" and question is None:
                question = m.content
        if not question or not sql:
            return "No completed query to learn from. Ask a question first."

        llm = context.get("llm_gateway")
        if not llm:
            return "No LLM gateway available."
        config = context.get("config")
        model = (config.target if config else "") or "openai/gpt-4o"

        response = await llm.chat(
            model=model,
            messages=[
                {"role": "system", "content": DRAFT_SYSTEM_PROMPT},
                {"role": "user", "content": _draft_prompt(question, sql)},
            ],
        )
        try:
            draft = _parse_draft(response)
        except Exception as first_error:
            # One repair round: feed the parse error back to the LLM.
            logger.warning("Could not parse LLM draft: %s; asking for repair", first_error)
            repair_response = await llm.chat(
                model=model,
                messages=[
                    {"role": "system", "content": DRAFT_SYSTEM_PROMPT},
                    {"role": "user", "content": _draft_prompt(question, sql)},
                    {"role": "assistant", "content": response},
                    {"role": "user", "content": (
                        f"Your YAML failed to parse: {first_error}\n"
                        f"Output ONLY the corrected YAML."
                    )},
                ],
            )
            try:
                draft = _parse_draft(repair_response)
            except Exception as e:
                logger.warning("Could not parse LLM draft after repair: %s", e)
                return f"Could not parse LLM draft after repair: {e}"

        kb.pending_draft = draft
        example = draft["example"]
        lines = [
            "Draft (run /kb learn --yes to save, or ignore to discard):",
            f"  question: {example['question']}",
            f"  sql: {example['sql'][:80]}",
            f"  tags: {example.get('tags', [])}",
        ]
        for term in draft["terms"]:
            lines.append(f"  term: {term.get('term', '')} → {term.get('mapping', '')}")
        return "\n".join(lines)

    async def _cmd_lessons(args: str) -> str:
        kb = context.get("kb")
        datasource = _datasource()
        if not datasource:
            return "No active datasource."

        if args.strip().endswith("--yes"):
            confirmed = await kb.confirm_pending_lessons(datasource)
            return f"Confirmed {confirmed} lesson(s) into the Hint Bank."

        await kb.ensure_synced(datasource)
        all_lessons = await kb.list_lessons(datasource, confirmed_only=False)
        confirmed = [l for l in all_lessons if l.get("confirmed")]
        pending = [l for l in all_lessons if not l.get("confirmed")]
        lines = [f"Hint Bank ({datasource}): {len(confirmed)} confirmed, {len(pending)} pending"]
        for l in confirmed[:10]:
            lines.append(f"  ✓ {l.get('pattern', '')}")
        for l in pending[:10]:
            lines.append(f"  ? {l.get('pattern', '')}  （/kb lessons --yes 确认）")
        if pending:
            lines.append("Pending lessons were auto-captured from successful corrections.")
        return "\n".join(lines)

    async def _cmd_init(kb, registry_svc, datasource, args) -> str:
        """LLM-assisted initialization: 描述由 LLM 起草,terms/examples 确定性生成。

        --docs <dir>:导入官方列描述(docs 权威,覆盖 LLM 草稿);
        低基数文本列先探测 distinct 值(样例进提示词,漏写列兜底合并)。
        """
        schema = await registry_svc.get_schema()
        llm = context.get("llm_gateway")

        if llm is None:
            # No LLM: plain skeleton (no descriptions)
            if kb.init_schema_notes(schema, datasource):
                return (
                    f"Created .trove/kb/{datasource}/schema_notes.yml skeleton. "
                    f"Fill in table/column descriptions, then /kb reload."
                )
            return f".trove/kb/{datasource}/schema_notes.yml already exists — refusing to overwrite."

        existing = kb.init_exists(datasource)
        if existing:
            return (
                f".trove/kb/{datasource}/ already has {', '.join(existing)} — "
                f"refusing to overwrite. Delete the files first to re-initialize."
            )

        config = context.get("config")
        model = (config.target if config else "") or "openai/gpt-4o"

        docs = load_docs_tables(Path(_docs_arg(args))) if _docs_arg(args) else {}
        lang = _lang_arg(args)

        # Live enum probe(离线/探测失败静默跳过,不影响 init)
        probed: dict = {}
        try:
            probed = await probe_enums(registry_svc, schema)
        except Exception:
            pass

        # Large schemas: draft per chunk (bounded LLM output), merge results.
        all_tables: list[dict] = []
        for chunk in _chunk_tables(schema):
            try:
                tables = await _draft_init_chunk(
                    llm, model, _table_dicts(chunk, docs), samples=probed,
                    lang=lang,
                )
            except Exception as e:
                logger.warning("Could not parse init draft after repair: %s", e)
                return f"Could not parse LLM draft after repair: {e}"
            all_tables.extend(tables)

        _backfill_types(all_tables, schema)
        all_tables = apply_docs(all_tables, docs)  # 官方描述权威
        if probed:
            all_tables = merge_into_notes({"tables": all_tables}, probed)["tables"]
        terms = generate_terms(all_tables, lang=lang)
        examples = generate_templates(all_tables, lang=lang)

        kb.init_notes(all_tables, datasource)
        kb.init_terms(terms, datasource)
        kb.init_examples(examples, datasource)
        await kb.force_sync(datasource)
        return (
            f"Initialized .trove/kb/{datasource}/: {len(all_tables)} tables annotated, "
            f"{len(terms)} terms, {len(examples)} templates. "
            f"Review the drafts, then /kb reload."
        )


    async def cmd_kb(args: str) -> str:
        sub = args.strip().split(maxsplit=1)[0] if args.strip() else ""
        kb = context.get("kb")
        if not kb:
            return "Knowledge base not available."
        datasource = _datasource()

        if sub == "init":
            registry_svc = context.get("connector_registry")
            if not registry_svc:
                return "No datasource connected."
            return await _cmd_init(kb, registry_svc, datasource, args)

        if sub == "list":
            await kb.ensure_synced(datasource or None)
            grouped = await kb.list_items()
            if not grouped:
                return "Knowledge base is empty. Run /kb init to create the schema skeleton."
            lines = ["Knowledge base items:"]
            for ds in sorted(grouped):
                lines.append(f"  {ds}:")
                for kind in sorted(grouped[ds]):
                    lines.append(f"    {kind}: {grouped[ds][kind]}")
            return "\n".join(lines)

        if sub == "reload":
            await kb.force_sync(datasource or None)
            grouped = await kb.list_items()
            total = sum(sum(kinds.values()) for kinds in grouped.values())
            return f"Knowledge base reloaded ({total} items)."

        if sub == "lessons":
            return await _cmd_lessons(args)

        if sub == "learn":
            return await _cmd_learn(args)

        return (
            "Usage: /kb init [--docs <dir>] [--lang en|zh] | list | reload | learn [--yes] | lessons [--yes]\n"
            "  init     draft table/column annotations via LLM, generate terms/templates "
            "deterministically; --docs imports official column descriptions; "
            "--lang sets the KB language (default en)\n"
            "  list     show knowledge base item counts per datasource\n"
            "  reload   re-sync YAML files immediately\n"
            "  learn    draft an example+terms from the last exchange; --yes saves it\n"
            "  lessons  show/confirm Hint Bank lessons from past corrections"
        )

    registry.register(SlashCommand(
        name="kb",
        description="Knowledge base: /kb init | list | reload | learn [--yes]",
        group="metadata",
        handler=cmd_kb,
    ))


async def _draft_init_chunk(llm, model, tables, samples=None, lang: str = "en") -> list:
    """Draft one schema chunk; on parse failure, one LLM repair round.

    Raises:
        Exception: Parse failure even after the repair round.
    """
    system = INIT_SYSTEM_PROMPT_ZH if lang == "zh" else INIT_SYSTEM_PROMPT
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": f"Schema:\n{_schema_text(tables, samples)}\n"},
    ]
    response = await llm.chat(
        model=model, messages=messages, max_tokens=INIT_MAX_TOKENS,
    )
    try:
        return _parse_init_tables(response)
    except Exception as first_error:
        logger.warning("Could not parse init draft: %s; asking for repair", first_error)
        repair_response = await llm.chat(
            model=model,
            max_tokens=INIT_MAX_TOKENS,
            messages=[
                *messages,
                {"role": "assistant", "content": response},
                {"role": "user", "content": (
                    f"Your YAML failed to parse: {first_error}\n"
                    f"Output ONLY the corrected YAML document."
                )},
            ],
        )
        return _parse_init_tables(repair_response)


def _docs_arg(args: str) -> str:
    """Extract --docs <path> from the command args (missing path → "")."""
    parts = args.split()
    if "--docs" in parts:
        i = parts.index("--docs")
        if i + 1 < len(parts):
            return parts[i + 1]
    return ""


def _lang_arg(args: str) -> str:
    """Extract --lang from the command args;默认英文(benchmark 均为英文问题)。"""
    parts = args.split()
    if "--lang" in parts:
        i = parts.index("--lang")
        if i + 1 < len(parts):
            return parts[i + 1]
    return "en"


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
