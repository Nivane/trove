"""REPL application — the main CLI interactive loop.

Uses prompt_toolkit for input handling (history, completion, syntax)
and Rich for output rendering.

Supports:
  - Natural language questions (any input not starting with /)
  - Slash commands (/help, /tables, /exit, etc.)
  - Streaming output for long-running queries
  - Ctrl+C to cancel running queries
  - Ctrl+D to exit
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.styles import Style

from trove.cli.tui import TUI
from trove.cli.slash_registry import SlashRegistry
from trove.cli.commands.session_cmds import register_session_commands
from trove.cli.commands.metadata_cmds import register_metadata_commands
from trove.cli.commands.system_cmds import register_system_commands

from trove.core.logging import get_logger

logger = get_logger(__name__)

# prompt_toolkit style
REPL_STYLE = Style.from_dict({
    "prompt": "bold cyan",
    "separator": "dim",
})


class TroveREPL:
    """Main REPL application for Trove CLI."""

    def __init__(
        self,
        session_manager: Any = None,
        config: Any = None,
        catalog_service: Any = None,
        connector_registry: Any = None,
        session_store: Any = None,
        current_session: Any = None,
    ):
        self._manager = session_manager
        self._config = config
        self._catalog = catalog_service
        self._registry = connector_registry
        self._store = session_store
        self._session = current_session

        self._tui = TUI()
        self._slash_registry = SlashRegistry()

        # Build shared context for command handlers
        self._context = {
            "session_manager": self._manager,
            "config": self._config,
            "catalog_service": self._catalog,
            "connector_registry": self._registry,
            "session_store": self._store,
            "current_session": self._session,
        }

        # Register all slash commands
        register_session_commands(self._slash_registry, self._context)
        register_metadata_commands(self._slash_registry, self._context)
        register_system_commands(self._slash_registry, self._context)

        # prompt_toolkit session with history
        self._prompt_session = PromptSession(
            history=FileHistory(".trove_history"),
            style=REPL_STYLE,
        )

        self._running = False

    # ── Main loop ────────────────────────────────────────

    async def run(self) -> None:
        """Start the REPL loop."""
        self._running = True
        self._tui.print_welcome()

        if self._session is None and self._manager:
            try:
                self._session = await self._manager.start_session()
                self._context["current_session"] = self._session
            except Exception as e:
                self._tui.print_error(f"Failed to create session: {e}")
                return

        while self._running:
            try:
                user_input = await self._prompt_session.prompt_async(
                    [("class:prompt", "trove> ")],
                )
                user_input = user_input.strip()
                if not user_input:
                    continue

                await self._handle_input(user_input)

            except (EOFError, KeyboardInterrupt):
                self._tui.print_info("Goodbye!")
                self._running = False
                break
            except Exception as e:
                self._tui.print_error(f"Unexpected error: {e}")
                logger.exception("REPL error")

    # ── Input handling ───────────────────────────────────

    async def _handle_input(self, text: str) -> None:
        """Route input to slash command or natural language query.

        Args:
            text: User input string.
        """
        if text.startswith("/"):
            await self._handle_slash(text)
        else:
            await self._handle_query(text)

    async def _handle_slash(self, text: str) -> None:
        """Handle a slash command.

        Args:
            text: Full slash command string (e.g. "/tables").
        """
        parts = text[1:].split(maxsplit=1)
        cmd_name = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        cmd = self._slash_registry.get(cmd_name)
        if cmd is None:
            self._tui.print_error(
                f"Unknown command: /{cmd_name}. Type /help for available commands."
            )
            return

        if cmd.name == "exit" or cmd.name in ("quit", "q"):
            self._running = False
            self._tui.print_info("Goodbye!")
            return

        try:
            result = await cmd.handler(args)
            if result:
                self._tui.print_help_text(result)
        except Exception as e:
            self._tui.print_error(f"Command failed: {e}")
            logger.exception("Slash command error: %s", cmd_name)

    async def _handle_query(self, text: str) -> None:
        """Handle a natural language query.

        Sends the query through the session manager's graph
        and displays results with streaming where possible.

        Args:
            text: Natural language question.
        """
        if self._manager is None or self._session is None:
            self._tui.print_error("No active session. Type /exit and restart.")
            return

        self._tui.print_separator()

        # Run the stream in a task so Ctrl+C can cancel the query
        # (asyncio cancellation propagates through the graph run).
        task = asyncio.create_task(self._consume_stream(text))
        try:
            await task
        except (KeyboardInterrupt, asyncio.CancelledError):
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            self._tui.print_info("Query cancelled.")

        self._tui.print_separator()

    async def _consume_stream(self, text: str) -> None:
        """Consume graph-native stream events and render them."""
        try:
            async for event in self._manager.ask_stream(
                session=self._session,
                question=text,
            ):
                event_type = event.get("type", "")
                content = event.get("content", "")

                if event_type == "thought":
                    self._tui.print_thought(content)
                elif event_type == "sql":
                    self._tui.print_sql(content)
                elif event_type == "result":
                    # Extract structured result from the workflow nodes
                    await self._display_detailed_results()
                elif event_type == "done":
                    self._tui.print_markdown(content)
                elif event_type == "error":
                    self._tui.print_error(content)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._tui.print_error(f"Query failed: {e}")
            logger.exception("Query error")

    async def _display_detailed_results(self) -> None:
        """Display detailed results from the last workflow run."""
        if not self._session:
            return

        # The latest assistant message contains the result data
        for msg in reversed(self._session.messages):
            if msg.role == "assistant" and msg.metadata.get("sql"):
                # SQL was already displayed via streaming
                pass

    # ── Cleanup ──────────────────────────────────────────

    async def cleanup(self) -> None:
        """Save session and clean up resources."""
        if self._session and self._manager:
            try:
                await self._manager.save_session(self._session)
            except Exception as e:
                logger.warning("Failed to save session on exit: %s", e)
