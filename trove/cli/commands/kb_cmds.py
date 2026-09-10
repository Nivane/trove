"""Knowledge base commands: /kb init | list | reload | learn [--yes].

/kb learn implements semi-automatic evolution: the LLM drafts an
example (+ candidate terms) from the last exchange, the draft is shown
and only written after the user confirms with /kb learn --yes.
"""

from __future__ import annotations

from trove.cli.slash_registry import SlashRegistry, SlashCommand
from trove.core.errors import DatasourceError
from trove.core.logging import get_logger
from trove.prompts import render
# init 管线(起草/修复/合成/回填)迁至 services/kb/init_pipeline.py,REPL
# 与 admin API 共用;_parse_init_tables/_recover_init_tables 与 chunk 常量
# 在此 re-export 保持测试导入面不变。
from trove.services.kb.init_pipeline import (
    INIT_CHUNK_TABLES,
    INIT_MAX_TOKENS,
    _parse_draft,
    _parse_init_tables,
    _recover_draft,
    _recover_init_tables,
    init_kb,
)

logger = get_logger(__name__)


def _draft_prompt(question: str, sql: str) -> str:
    """/kb learn 用户提示词（薄封装，模板见 prompts/kb/draft_user.en.j2）。"""
    return render("kb/draft_user", question=question, sql=sql)


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
            await kb.append_example(
                draft["example"], datasource, generator="kb_learn",
            )
            for term in draft["terms"]:
                await kb.append_term(term, datasource, generator="kb_learn")
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
                {"role": "system", "content": render("kb/draft_system")},
                {"role": "user", "content": _draft_prompt(question, sql)},
            ],
        )
        try:
            draft = _parse_draft(response)
        except Exception as first_error:
            # 推理模型可能把全部输出写进 reasoning(网关回退 prose):围栏内
            # 草稿直接回收,不触发修复轮。修复轮也不再回声 prose 进上下文。
            draft = _recover_draft(response)
            if draft is None:
                logger.warning("Could not parse LLM draft: %s; asking for repair", first_error)
                repair_response = await llm.chat(
                    model=model,
                    messages=[
                        {"role": "system", "content": render("kb/draft_system")},
                        {"role": "user", "content": _draft_prompt(question, sql)},
                        {"role": "user", "content": (
                            f"Your YAML failed to parse: {first_error}\n"
                            f"Output ONLY the corrected YAML."
                        )},
                    ],
                )
                try:
                    draft = _parse_draft(repair_response)
                except Exception:
                    draft = _recover_draft(repair_response)
                if draft is None:
                    logger.warning("Could not parse LLM draft after repair: %s", first_error)
                    return f"Could not parse LLM draft after repair: {first_error}"

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
        """LLM-assisted initialization — 薄封装:管线在 init_pipeline.init_kb。

        --docs <dir>:导入官方列描述(docs 权威,覆盖 LLM 草稿);
        --overwrite:重新生成,替换已存在的 KB 文件(旧 flat terms: 格式
        迁移的唯一路径);
        低基数文本列先探测 distinct 值(样例进提示词,漏写列兜底合并);
        统计 profiling 每列写入 stats(null 比例/distinct/极值/值形状),
        统计证据随 schema_text 进 LLM 起草提示词(AskData 式总结);
        合成 few-shot:LLM 基于统计生成 Q/SQL 对,经 SQLGlot + 试执行
        双护栏后追加进 examples.yml(纯合成,零金标注入)。
        """
        llm = context.get("llm_gateway")
        config = context.get("config")
        try:
            return await init_kb(
                kb, registry_svc, llm, config, datasource,
                overwrite=_overwrite_arg(args),
                force=_force_arg(args),
                docs=_docs_arg(args),
                lang=_lang_arg(args),
            )
        except DatasourceError as e:
            return e.message


    _MIGRATE_USAGE = (
        "Usage: /kb migrate [--check|--keep]  (read-only: never writes a file, "
        "never calls the LLM)\n"
        "Migrating an asset has two options, and neither is automatic:\n"
        "  keep as-is   leave the files alone (the default; /kb migrate --keep says so "
        "explicitly). Nothing is lost, and nothing is migrated.\n"
        "  regenerate   /kb init --overwrite --force — replaces the files from the "
        "datasource (LLM-billed), and establishes the baseline so later re-inits "
        "**merge** your edits instead of replacing them.\n"
        "Without a baseline the two cannot be told apart, which is why the choice is "
        "yours. Everything else (/kb migrate) is read-only."
    )

    def _report_assets(kb, datasource: str) -> str:
        """单数据源的资产版本体检(只读,永不写盘)。

        "只做判断 + 报告"是刻意的:读路径在每次查询里,不能写文件;而格式
        迁移会改人的资产,必须有一次显式的"我同意"。
        """
        report = kb.asset_report(datasource)
        if not report:
            return f"No KB assets found for '{datasource}'."
        lines = [f"KB assets for '{datasource}':"]
        pending = edited = 0
        for asset in report:
            if asset.get("error"):
                lines.append(f"  {asset['file']}: unreadable — {asset['error']}")
                continue
            if asset.get("refused"):
                lines.append(
                    f"  {asset['file']}: REFUSED (format {asset['format']}) — "
                    f"upgrade Trove; the mirror keeps the last version it could read"
                )
                continue
            fmt = asset["format"]
            state = "current" if not asset.get("needs_migration") else "needs migration"
            edited_note = {
                True: "hand-edited", False: "as generated", None: "no digest (unknown)",
            }[asset["edited"]]
            mergeable = (
                "re-init merges" if asset.get("has_baseline")
                else "re-init CANNOT merge (no baseline)"
            )
            lines.append(
                f"  {asset['file']}: format {fmt} ({state}), {edited_note}, "
                f"generated by {asset['generator'] or 'unknown'} — {mergeable}"
            )
            if asset.get("needs_migration"):
                pending += 1
                if asset["edited"] is not False:
                    edited += 1
        if pending:
            lines.append(
                f"{pending} asset(s) would be migrated; {edited} of them may carry "
                f"human edits (merge, not overwrite)."
            )
        else:
            lines.append("All assets are at the current format — nothing to migrate.")
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

        if sub == "migrate":
            # **migrate 本身零 LLM 调用**:它是一个体检 + 决策的道具,不是
            # 一个会自己花钱的动作。真正的重生成是 /kb init --overwrite --force,
            # 由人显式敲出来(design §3.3:这一次需要人拍板)。
            extra = " ".join(
                w for w in args.split()[1:] if w not in ("--check", "--keep")
            ).strip()
            if extra:
                return (
                    f"Unknown flag: {extra}\n" + _MIGRATE_USAGE
                )
            if "--keep" in args and "--check" not in args:
                return (
                    "Keeping the files exactly as they are; nothing was written. "
                    "Nothing to do until you regenerate once (see /kb migrate --check)."
                )
            await kb.ensure_synced(datasource or None)
            targets = (
                [datasource] if datasource
                else [p.name for p in kb._datasource_dirs()]
            )
            report = (
                "\n\n".join(_report_assets(kb, ds) for ds in targets)
                if targets else "No datasources with a KB. Run /kb init first."
            )
            # --check 是纯体检;不带 flag = 体检 + 两个选项(默认什么都不做)
            if "--check" in args:
                return report
            return report + "\n\n" + _MIGRATE_USAGE

        if sub == "lessons":
            return await _cmd_lessons(args)

        if sub == "learn":
            return await _cmd_learn(args)

        return (
            "Usage: /kb init [--docs <dir>] [--lang en|zh] [--overwrite [--force]] | "
            "list | reload | learn [--yes] | lessons [--yes] | migrate [--check|--keep]\n"
            "  init     draft table/column annotations via LLM, generate terms/templates "
            "deterministically; --docs imports official column descriptions; "
            "--lang sets the KB language (default en); --overwrite re-generates, "
            "**merging** into existing files (your edits survive entry by entry); "
            "--force replaces them outright (needed once to migrate a KB that has no "
            "baseline yet)\n"
            "  list     show knowledge base item counts per datasource\n"
            "  reload   re-sync YAML files immediately\n"
            "  learn    draft an example+terms from the last exchange; --yes saves it\n"
            "  lessons  show/confirm Hint Bank lessons from past corrections\n"
            "  migrate  report each asset's format version, whether it carries human "
            "edits, and whether it can be merged (read-only; never calls the LLM)"
        )

    registry.register(SlashCommand(
        name="kb",
        description="Knowledge base: /kb init | list | reload | learn [--yes]",
        group="metadata",
        handler=cmd_kb,
    ))


def _docs_arg(args: str) -> str:
    """Extract --docs <path> from the command args (missing path → "")."""
    parts = args.split()
    if "--docs" in parts:
        i = parts.index("--docs")
        if i + 1 < len(parts):
            return parts[i + 1]
    return ""


def _overwrite_arg(args: str) -> bool:
    """--overwrite 存在即真:重新生成已存在的 KB 文件。

    C2 起它的含义是**合并**已存在的文件(人的编辑按条目存活),不是覆盖;
    真正的覆盖要再给一个 --force。
    """
    return "--overwrite" in args.split()


def _force_arg(args: str) -> bool:
    """--force 存在即真:覆盖写(今天的行为)。无基线时唯一能往前走的一步。"""
    return "--force" in args.split()


def _lang_arg(args: str) -> str:
    """Extract --lang from the command args;默认英文(benchmark 均为英文问题)。"""
    parts = args.split()
    if "--lang" in parts:
        i = parts.index("--lang")
        if i + 1 < len(parts):
            return parts[i + 1]
    return "en"
