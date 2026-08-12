"""Slash command registry tests."""

import pytest

from trove.cli.slash_registry import SlashRegistry, SlashCommand
from trove.cli.commands.session_cmds import register_session_commands
from trove.cli.commands.metadata_cmds import register_metadata_commands
from trove.cli.commands.system_cmds import register_system_commands


@pytest.fixture
def registry():
    reg = SlashRegistry()
    register_session_commands(reg, {})
    register_metadata_commands(reg, {})
    register_system_commands(reg, {})
    return reg


class TestRegistryBasics:
    def test_register_and_get(self):
        reg = SlashRegistry()
        async def handler(args):
            return "ok"
        reg.register(SlashCommand(
            name="test", description="test cmd", group="session", handler=handler,
        ))
        cmd = reg.get("test")
        assert cmd is not None
        assert cmd.name == "test"

    def test_get_unknown(self):
        reg = SlashRegistry()
        assert reg.get("nonexistent") is None

    def test_alias_resolution(self):
        reg = SlashRegistry()
        async def handler(args):
            return "ok"
        reg.register(SlashCommand(
            name="quit", description="q", group="session",
            handler=handler, aliases=["q"],
        ))
        assert reg.get("q").name == "quit"

    def test_list_by_group(self):
        reg = SlashRegistry()
        async def h(args):
            return ""
        reg.register(SlashCommand(name="a", description="", group="g1", handler=h))
        reg.register(SlashCommand(name="b", description="", group="g2", handler=h))
        reg.register(SlashCommand(name="c", description="", group="g1", handler=h))
        assert len(reg.list_by_group("g1")) == 2
        assert len(reg.list_by_group("g2")) == 1

    def test_groups(self):
        reg = SlashRegistry()
        async def h(args):
            return ""
        reg.register(SlashCommand(name="a", description="", group="g1", handler=h))
        reg.register(SlashCommand(name="b", description="", group="g2", handler=h))
        assert set(reg.groups()) == {"g1", "g2"}


class TestBuiltInCommands:
    def test_session_commands_registered(self, registry):
        for name in ["help", "exit", "clear", "compact"]:
            assert registry.get(name) is not None, f"Missing /{name}"

    def test_metadata_commands_registered(self, registry):
        for name in ["tables", "table_schema", "schemas", "databases"]:
            assert registry.get(name) is not None, f"Missing /{name}"

    def test_system_commands_registered(self, registry):
        for name in ["model", "datasource", "init"]:
            assert registry.get(name) is not None, f"Missing /{name}"

    def test_all_have_descriptions(self, registry):
        for cmd in registry.list_all():
            assert cmd.description, f"/{cmd.name} has no description"

    def test_command_groups_valid(self, registry):
        valid_groups = {"session", "metadata", "system"}
        for cmd in registry.list_all():
            assert cmd.group in valid_groups, f"/{cmd.name} has invalid group"

    async def test_help_handler(self, registry):
        cmd = registry.get("help")
        result = await cmd.handler("")
        assert "Available commands" in result

    async def test_exit_handler(self, registry):
        cmd = registry.get("exit")
        result = await cmd.handler("")
        assert result


class TestCommandHandlersWithContext:
    async def test_clear_with_session(self, tmp_home):
        from trove.core.types import Message, Session
        from trove.storage.session_store import SessionStore

        store = SessionStore(home_dir=str(tmp_home))
        session = await store.create_session(project_cwd="/tmp/p")
        session.messages.append(Message(role="user", content="q"))

        context = {
            "current_session": session,
            "session_store": store,
        }

        reg = SlashRegistry()
        register_session_commands(reg, context)

        result = await reg.get("clear").handler("")
        assert "cleared" in result.lower()
        assert session.messages == []

    async def test_model_command_set(self):
        class FakeConfig:
            target = "old-model"

        context = {"config": FakeConfig()}
        reg = SlashRegistry()
        register_system_commands(reg, context)

        # Show current
        result = await reg.get("model").handler("")
        assert "old-model" in result

        # Set new
        result = await reg.get("model").handler("new-model")
        assert "new-model" in result
        assert context["config"].target == "new-model"

    async def test_datasource_command_list(self, sqlite_registry):
        context = {"connector_registry": sqlite_registry}
        reg = SlashRegistry()
        register_system_commands(reg, context)

        result = await reg.get("datasource").handler("")
        assert "test_db" in result

    async def test_datasource_switch(self, sqlite_registry):
        context = {"connector_registry": sqlite_registry}
        reg = SlashRegistry()
        register_system_commands(reg, context)

        result = await reg.get("datasource").handler("test_db")
        assert "Switched" in result

    async def test_datasource_switch_unknown(self, sqlite_registry):
        context = {"connector_registry": sqlite_registry}
        reg = SlashRegistry()
        register_system_commands(reg, context)

        result = await reg.get("datasource").handler("ghost")
        assert "not found" in result

    async def test_tables_command(self, sqlite_registry):
        from trove.services.datasource.catalog import CatalogService
        context = {"catalog_service": CatalogService(sqlite_registry)}
        reg = SlashRegistry()
        register_metadata_commands(reg, context)

        result = await reg.get("tables").handler("")
        assert "students" in result

    async def test_table_schema_command(self, sqlite_registry):
        from trove.services.datasource.catalog import CatalogService
        context = {"catalog_service": CatalogService(sqlite_registry)}
        reg = SlashRegistry()
        register_metadata_commands(reg, context)

        result = await reg.get("table_schema").handler("students")
        assert "grade" in result
        assert "county" in result

    async def test_table_schema_missing_args(self, sqlite_registry):
        from trove.services.datasource.catalog import CatalogService
        context = {"catalog_service": CatalogService(sqlite_registry)}
        reg = SlashRegistry()
        register_metadata_commands(reg, context)

        result = await reg.get("table_schema").handler("")
        assert "Usage" in result
