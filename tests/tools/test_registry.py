"""Tool registry tests."""

import pytest

from trove.tools.registry import ToolRegistry, ToolDefinition


def make_tool(name="my_tool", **kwargs):
    defaults = dict(
        name=name,
        description="Test tool",
        parameters={"type": "object", "properties": {}},
        handler=lambda: "result",
        source="builtin",
        permissions=["read_only"],
    )
    defaults.update(kwargs)
    return ToolDefinition(**defaults)


class TestToolDefinition:
    def test_create(self):
        tool = make_tool()
        assert tool.name == "my_tool"
        assert tool.description == "Test tool"
        assert tool.source == "builtin"
        assert "read_only" in tool.permissions


class TestToolRegistry:
    def test_register_and_get(self):
        reg = ToolRegistry()
        reg.register(make_tool("t1"))
        assert reg.get("t1").name == "t1"

    def test_get_missing(self):
        reg = ToolRegistry()
        assert reg.get("nope") is None

    def test_register_overwrite(self):
        reg = ToolRegistry()
        reg.register(make_tool("dup", description="first"))
        reg.register(make_tool("dup", description="second"))
        assert reg.get("dup").description == "second"

    def test_register_many(self):
        reg = ToolRegistry()
        reg.register_many([make_tool("a"), make_tool("b"), make_tool("c")])
        assert reg.count == 3

    def test_unregister(self):
        reg = ToolRegistry()
        reg.register(make_tool("t1"))
        assert reg.unregister("t1") is True
        assert reg.unregister("t1") is False

    def test_list_all(self):
        reg = ToolRegistry()
        reg.register_many([make_tool("a"), make_tool("b")])
        names = sorted(t.name for t in reg.list_all())
        assert names == ["a", "b"]

    def test_list_names(self):
        reg = ToolRegistry()
        reg.register_many([make_tool("x"), make_tool("y")])
        assert set(reg.list_names()) == {"x", "y"}

    def test_list_by_permission(self):
        reg = ToolRegistry()
        reg.register(make_tool("ro1", permissions=["read_only"]))
        reg.register(make_tool("rw1", permissions=["read_write"]))
        reg.register(make_tool("d1", permissions=["dangerous"]))

        ro = reg.list_by_permission("read_only")
        assert len(ro) == 1
        assert ro[0].name == "ro1"

    def test_list_by_source(self):
        reg = ToolRegistry()
        reg.register(make_tool("builtin1", source="builtin"))
        reg.register(make_tool("external1", source="mcp_client"))

        builtin = reg.list_by_source("builtin")
        assert len(builtin) == 1
        assert builtin[0].name == "builtin1"

        external = reg.list_by_source("mcp_client")
        assert len(external) == 1
        assert external[0].name == "external1"

    def test_list_for_subagent_returns_builtin(self):
        reg = ToolRegistry()
        reg.register(make_tool("builtin1", source="builtin"))
        reg.register(make_tool("external1", source="mcp_client"))

        tools = reg.list_for_subagent("any_subagent")
        assert len(tools) == 1
        assert tools[0].name == "builtin1"

    def test_to_mcp_tools(self):
        reg = ToolRegistry()
        reg.register(make_tool("tool1"))

        mcp_tools = reg.to_mcp_tools()
        assert len(mcp_tools) == 1
        assert mcp_tools[0]["name"] == "tool1"
        assert mcp_tools[0]["description"] == "Test tool"
        assert "inputSchema" in mcp_tools[0]

    def test_clear(self):
        reg = ToolRegistry()
        reg.register(make_tool("a"))
        reg.clear()
        assert reg.count == 0
