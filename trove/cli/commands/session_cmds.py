"""Session commands: /help, /exit, /clear, /compact."""

from trove.cli.slash_registry import SlashRegistry, SlashCommand


def register_session_commands(registry: SlashRegistry, context: dict) -> None:
    """Register all session-related commands.

    Args:
        registry: The slash command registry.
        context: Dict with 'session_manager', 'current_session', etc.
    """

    async def cmd_help(args: str) -> str:
        """Show available commands and their descriptions."""
        lines = ["Available commands:\n"]
        for group in registry.groups():
            lines.append(f"  [{group}]")
            for cmd in registry.list_by_group(group):
                lines.append(f"    /{cmd.name:<20} {cmd.description}")
        lines.append("\n  Type a question directly to query the database.")
        return "\n".join(lines)

    async def cmd_exit(args: str) -> str:
        """Exit the REPL."""
        return "Goodbye!"

    async def cmd_clear(args: str) -> str:
        """Clear the current session (start fresh)."""
        session = context.get("current_session")
        if session:
            session.messages.clear()
            store = context.get("session_store")
            if store:
                await store.save_session(session)
        return "Session cleared. Starting fresh."

    async def cmd_compact(args: str) -> str:
        """Compress conversation history to save context space."""
        manager = context.get("session_manager")
        session = context.get("current_session")
        if not manager or not session:
            return "No active session to compact."

        try:
            compacted = await manager.compact_session(session)
            context["current_session"] = compacted
            return f"Session compacted. {len(compacted.messages)} messages remain."
        except Exception as e:
            return f"Compaction failed: {e}"

    registry.register(SlashCommand(
        name="help",
        description="Show this help message",
        group="session",
        handler=cmd_help,
        aliases=["h", "?"],
    ))
    registry.register(SlashCommand(
        name="exit",
        description="Exit the REPL",
        group="session",
        handler=cmd_exit,
        aliases=["quit", "q"],
    ))
    registry.register(SlashCommand(
        name="clear",
        description="Clear current session history",
        group="session",
        handler=cmd_clear,
    ))
    registry.register(SlashCommand(
        name="compact",
        description="Compress conversation history",
        group="session",
        handler=cmd_compact,
    ))

    async def cmd_tasks(args: str) -> str:
        """Show the current session's cross-turn task list."""
        manager = context.get("session_manager")
        session = context.get("current_session")
        if not manager or not session:
            return "No active session."
        lang = getattr(context.get("config"), "language", "zh")
        tasks = await manager.get_tasks(session)
        if not tasks:
            return ("当前会话没有任务。" if lang == "zh"
                    else "No tasks in the current session.")
        marks = {
            "pending": "·", "in_progress": "→", "done": "✓",
            "failed": "✗", "skipped": "-",
        }
        lines = []
        for t in tasks:
            status = t.get("status", "pending")
            mark = marks.get(status, "·")
            title = (t.get("title") or "")[:80]
            failed_reason = ""
            meta = t.get("metadata") or {}
            if status == "failed" and meta.get("error"):
                failed_reason = f" ({meta['error'][:60]})"
            lines.append(f"{mark} {t.get('position', 0) + 1}. {title} [{status}]{failed_reason}")
        return "\n".join(lines)

    registry.register(SlashCommand(
        name="tasks",
        description="Show cross-turn task list",
        group="session",
        handler=cmd_tasks,
        aliases=["todo"],
    ))
