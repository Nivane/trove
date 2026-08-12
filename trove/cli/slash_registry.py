"""Slash command registry for the REPL.

Groups commands by category:
  - Session: /help, /exit, /clear, /compact
  - Metadata: /tables, /schemas, /table_schema
  - System: /model, /datasource, /init
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

Handler = Callable[..., Awaitable[str]]


@dataclass
class SlashCommand:
    """A registered slash command."""

    name: str
    description: str
    group: str  # "session", "metadata", "system"
    handler: Handler
    usage: str = ""
    aliases: list[str] = field(default_factory=list)


class SlashRegistry:
    """Registry of all slash commands available in the REPL."""

    def __init__(self):
        self._commands: dict[str, SlashCommand] = {}
        self._aliases: dict[str, str] = {}

    def register(self, cmd: SlashCommand) -> None:
        """Register a slash command."""
        self._commands[cmd.name] = cmd
        for alias in cmd.aliases:
            self._aliases[alias] = cmd.name

    def get(self, name: str) -> SlashCommand | None:
        """Get a command by name or alias."""
        resolved = self._aliases.get(name, name)
        return self._commands.get(resolved)

    def list_by_group(self, group: str) -> list[SlashCommand]:
        """List commands in a group."""
        return [c for c in self._commands.values() if c.group == group]

    def list_all(self) -> list[SlashCommand]:
        """List all registered commands."""
        return list(self._commands.values())

    def groups(self) -> list[str]:
        """List all command groups."""
        seen = set()
        groups = []
        for cmd in self._commands.values():
            if cmd.group not in seen:
                seen.add(cmd.group)
                groups.append(cmd.group)
        return groups
