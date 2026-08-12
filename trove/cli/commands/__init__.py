"""CLI command implementations."""

from trove.cli.commands.session_cmds import register_session_commands
from trove.cli.commands.metadata_cmds import register_metadata_commands
from trove.cli.commands.system_cmds import register_system_commands
from trove.cli.slash_registry import SlashRegistry

__all__ = [
    "register_session_commands",
    "register_metadata_commands",
    "register_system_commands",
]
