"""Tool registry — unified tool definition and discovery.

All tools (builtin, MCP client, skill, plugin) register here.
In MVP, only builtin tools are registered.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from trove.core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class ToolDefinition:
    """A tool that can be called by an Agent or exposed via MCP."""

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)
    # JSON Schema for function parameters
    handler: Callable[..., Any] | None = None
    source: Literal["builtin", "mcp_client", "skill", "plugin"] = "builtin"
    permissions: list[str] = field(default_factory=list)
    # e.g. ["read_only", "read_write", "dangerous"]


class ToolRegistry:
    """Central registry for all tools available to the Agent.

    Tools are registered with a name and definition.
    The registry filters tools by subagent, permission level, etc.
    """

    def __init__(self):
        self._tools: dict[str, ToolDefinition] = {}

    # ── Registration ─────────────────────────────────────

    def register(self, tool: ToolDefinition) -> None:
        """Register a tool definition.

        Args:
            tool: The tool to register.
        """
        if tool.name in self._tools:
            logger.warning("Overwriting tool: %s", tool.name)
        self._tools[tool.name] = tool
        logger.debug("Registered tool: %s (%s)", tool.name, tool.source)

    def register_many(self, tools: list[ToolDefinition]) -> None:
        """Register multiple tools at once."""
        for tool in tools:
            self.register(tool)

    def unregister(self, name: str) -> bool:
        """Remove a tool by name.

        Returns:
            True if removed, False if not found.
        """
        if name in self._tools:
            del self._tools[name]
            return True
        return False

    # ── Discovery ────────────────────────────────────────

    def get(self, name: str) -> ToolDefinition | None:
        """Get a tool by name."""
        return self._tools.get(name)

    def list_all(self) -> list[ToolDefinition]:
        """List all registered tools."""
        return list(self._tools.values())

    def list_names(self) -> list[str]:
        """List all tool names."""
        return list(self._tools.keys())

    def list_by_permission(
        self,
        level: str,
    ) -> list[ToolDefinition]:
        """Filter tools by permission level.

        Args:
            level: "read_only", "read_write", or "dangerous".

        Returns:
            Tools that match the permission.
        """
        results = []
        for tool in self._tools.values():
            if level in tool.permissions:
                results.append(tool)
            elif "dangerous" in tool.permissions and level == "dangerous":
                results.append(tool)
        return results

    def list_by_source(
        self,
        source: Literal["builtin", "mcp_client", "skill", "plugin"],
    ) -> list[ToolDefinition]:
        """Filter tools by source."""
        return [t for t in self._tools.values() if t.source == source]

    def list_for_subagent(self, subagent_id: str) -> list[ToolDefinition]:
        """Get tools available to a specific subagent.

        In MVP, returns all builtin tools since there's only one agent.
        In v0.2+, this filters by subagent-specific tool whitelists.

        Args:
            subagent_id: The subagent identifier.

        Returns:
            List of ToolDefinitions available to this subagent.
        """
        # MVP: all builtin tools are available
        return self.list_by_source("builtin")

    # ── MCP export ───────────────────────────────────────

    def to_mcp_tools(self) -> list[dict[str, Any]]:
        """Export all tools as MCP tools/list format.

        Returns:
            List of dicts compatible with MCP tools/list response.
        """
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.parameters,
            }
            for tool in self._tools.values()
        ]

    # ── Info ─────────────────────────────────────────────

    @property
    def count(self) -> int:
        return len(self._tools)

    def clear(self) -> None:
        """Remove all registered tools."""
        self._tools.clear()
