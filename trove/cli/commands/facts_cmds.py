"""User memory commands: /facts | /facts add <text> | /facts del <id>.

Personalization memory (Mem0-style): per-user preference/caliber facts,
scoped to the active datasource, injected into SQL generation. The REPL
runs as the "local" user; the API/CLI's authenticated users each carry
their own facts.
"""

from __future__ import annotations

from trove.cli.slash_registry import SlashRegistry, SlashCommand

USAGE = "用法: /facts [datasource]  |  /facts add <口径/偏好文本>  |  /facts del <id>"


def register_facts_commands(registry: SlashRegistry, context: dict) -> None:
    """Register the /facts command (subcommands: list, add, del)."""

    def _datasource() -> str:
        registry_svc = context.get("connector_registry")
        if registry_svc is None:
            return ""
        return registry_svc.default_name or ""

    async def cmd_facts(args: str) -> str:
        svc = context.get("user_facts")
        if svc is None:
            return "User facts service unavailable."
        user_id = "local"
        parts = args.split()
        if parts and parts[0] == "add":
            text = " ".join(parts[1:]).strip()
            ds = _datasource()
            if not text:
                return USAGE
            if not ds:
                return "No active datasource to attach the fact to. Connect a datasource first."
            row = await svc.add(user_id, ds, text)
            return f"Saved fact #{row['id']} ({ds}): {row['fact']}"
        if parts and parts[0] == "del":
            if len(parts) != 2 or not parts[1].isdigit():
                return USAGE
            if await svc.delete(user_id, int(parts[1])):
                return f"Deleted fact #{parts[1]}."
            return f"No fact #{parts[1]}."
        datasource = args.strip() or None
        facts = await svc.list(user_id, datasource)
        if not facts:
            scope = datasource or _datasource() or "?"
            return f"No facts for user '{user_id}' on '{scope}'. Add one with: /facts add <text>"
        lines = [f"用户 '{user_id}' 的事实({len(facts)}):"]
        for f in facts:
            lines.append(f"  #{f['id']} [{f['datasource']}] {f['fact']}")
        return "\n".join(lines)

    registry.register(SlashCommand(
        name="facts",
        description="User memory (preferences/calibers): list / add / del",
        group="system",
        handler=cmd_facts,
        usage=USAGE,
        aliases=["fact"],
    ))
