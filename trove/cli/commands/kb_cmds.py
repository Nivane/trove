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
            schema = await registry_svc.get_schema()
            if kb.init_schema_notes(schema, datasource):
                return (
                    f"Created .trove/kb/{datasource}/schema_notes.yml skeleton. "
                    f"Fill in table/column descriptions, then /kb reload."
                )
            return f".trove/kb/{datasource}/schema_notes.yml already exists — refusing to overwrite."

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
