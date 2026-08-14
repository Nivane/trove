"""Knowledge base commands: /kb init | list | reload | learn [--yes].

/kb learn implements semi-automatic evolution: the LLM drafts an
example (+ candidate terms) from the last exchange, the draft is shown
and only written after the user confirms with /kb learn --yes.
"""

from __future__ import annotations

import yaml

from trove.cli.slash_registry import SlashRegistry, SlashCommand
from trove.core.logging import get_logger

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

INIT_SYSTEM_PROMPT = """You are initializing a knowledge base for a SQL data agent. Given the schema below, produce ONE YAML document with THREE top-level sections: tables, terms, examples (strict YAML, no markdown fences, no commentary, every value on a single line).

Section 1 — table/column annotations:
tables:
  - name: <table>
    description: <one-line business description in Chinese>
    columns:
      - name: <column>
        description: <one-line description in Chinese>
        enums: []
    metrics: []

Section 2 — business terms:
terms:
  - term: <Chinese business term>
    aliases: [<1-2 synonyms>]
    mapping: <SQL expression>
    tables: [<tables involved>]
    definition: <one line>

For every table add a row-count term; for every numeric column add a sensible SUM or AVG aggregation term.

Section 3 — reference templates:
examples:
  - template: true
    question: <generic Chinese question>
    sql: <generic SQL on one line>
    tags: [<2-3 tags>]

One or two generic templates per table (row count, group-by aggregate)."""


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


def _schema_text(tables) -> str:
    """Compact schema listing for the init prompt (table: col type, col type)."""
    lines = []
    for table in tables:
        cols = ", ".join(f"{c.name} {c.type}" for c in table.columns)
        lines.append(f"{table.name}: {cols}")
    return "\n".join(lines)


def _chunk_tables(schema, size: int | None = None):
    """Yield schema tables in chunks (large schemas exceed one LLM output)."""
    if size is None:
        size = INIT_CHUNK_TABLES  # read at call time (tests monkeypatch it)
    tables = schema.tables
    for i in range(0, len(tables), size):
        yield tables[i : i + size]


def _parse_init_docs(response: str) -> tuple[list, list, list]:
    """Parse the init draft into (tables, terms, examples).

    Accepts both shapes LLMs actually produce:
      - ONE document with three top-level sections (tables/terms/examples)
        — the common real-world output, extra top-level keys tolerated
      - three '---'-separated documents with one section each

    Raises:
        ValueError: Wrong document count or missing sections.
    """
    docs = [d for d in yaml.safe_load_all(_strip_fences(response)) if d]

    if len(docs) == 1 and isinstance(docs[0], dict) and all(
        key in docs[0] for key in ("tables", "terms", "examples")
    ):
        doc = docs[0]
        tables = doc["tables"]
        terms = doc["terms"]
        examples = doc["examples"]
    elif len(docs) == 3:
        tables = docs[0].get("tables") if isinstance(docs[0], dict) else None
        terms = docs[1].get("terms") if isinstance(docs[1], dict) else None
        examples = docs[2].get("examples") if isinstance(docs[2], dict) else None
    else:
        raise ValueError(f"expected one merged document or 3 YAML documents, got {len(docs)}")

    if not all(isinstance(x, list) for x in (tables, terms, examples)):
        raise ValueError("draft must contain 'tables' / 'terms' / 'examples' sections")
    return tables, terms, examples


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

    async def _cmd_init(kb, registry_svc, datasource) -> str:
        """LLM-assisted three-file initialization; skeleton fallback without LLM."""
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

        # Large schemas: draft per chunk (bounded LLM output), merge results.
        all_tables, all_terms, all_examples = [], [], []
        for chunk in _chunk_tables(schema):
            try:
                tables, terms, examples = await _draft_init_chunk(llm, model, chunk)
            except Exception as e:
                logger.warning("Could not parse init draft after repair: %s", e)
                return f"Could not parse LLM draft after repair: {e}"
            all_tables.extend(tables)
            all_terms.extend(terms)
            all_examples.extend(examples)

        kb.init_notes(all_tables, datasource)
        kb.init_terms(all_terms, datasource)
        kb.init_examples(all_examples, datasource)
        await kb.force_sync(datasource)
        return (
            f"Initialized .trove/kb/{datasource}/: {len(all_tables)} tables annotated, "
            f"{len(all_terms)} terms, {len(all_examples)} templates. "
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
            return await _cmd_init(kb, registry_svc, datasource)

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

        if sub == "learn":
            return await _cmd_learn(args)

        return (
            "Usage: /kb init | /kb list | /kb reload | /kb learn [--yes]\n"
            "  init    generate schema_notes.yml skeleton for the active datasource\n"
            "  list    show knowledge base item counts per datasource\n"
            "  reload  re-sync YAML files immediately\n"
            "  learn   draft an example+terms from the last exchange; --yes saves it"
        )

    registry.register(SlashCommand(
        name="kb",
        description="Knowledge base: /kb init | list | reload | learn [--yes]",
        group="metadata",
        handler=cmd_kb,
    ))


async def _draft_init_chunk(llm, model, tables) -> tuple[list, list, list]:
    """Draft one schema chunk; on parse failure, one LLM repair round.

    Raises:
        Exception: Parse failure even after the repair round.
    """
    messages = [
        {"role": "system", "content": INIT_SYSTEM_PROMPT},
        {"role": "user", "content": f"Schema:\n{_schema_text(tables)}\n"},
    ]
    response = await llm.chat(
        model=model, messages=messages, max_tokens=INIT_MAX_TOKENS,
    )
    try:
        return _parse_init_docs(response)
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
        return _parse_init_docs(repair_response)
