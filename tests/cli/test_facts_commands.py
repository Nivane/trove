"""User facts slash-command tests (/facts) + `trove admin facts` subcommands."""

from __future__ import annotations

import pytest

from trove.cli.slash_registry import SlashRegistry
from trove.cli.commands.facts_cmds import register_facts_commands
from trove.services.user_facts.service import UserFactsService


@pytest.fixture
async def svc(tmp_path):
    return UserFactsService(tmp_path / "user_facts.db")


class _FakeRegistry:
    default_name = "test_db"


def make_reg(svc, registry=None):
    reg = SlashRegistry()
    register_facts_commands(reg, {
        "user_facts": svc,
        "connector_registry": registry or _FakeRegistry(),
    })
    return reg


class TestFactsSlashCommand:
    def test_registered(self, svc):
        reg = make_reg(svc)
        assert reg.get("facts") is not None
        assert reg.get("fact") is not None  # alias

    async def test_add_and_list(self, svc):
        reg = make_reg(svc)
        out = await reg.get("facts").handler("add 营收 = 净收入")
        assert "Saved fact #1" in out
        out = await reg.get("facts").handler("")
        assert "营收 = 净收入" in out
        assert "[test_db]" in out

    async def test_delete(self, svc):
        reg = make_reg(svc)
        await reg.get("facts").handler("add 营收 = 净收入")
        out = await reg.get("facts").handler("del 1")
        assert "Deleted fact #1" in out
        out = await reg.get("facts").handler("del 1")
        assert "No fact #1" in out

    async def test_empty_list_hint(self, svc):
        reg = make_reg(svc)
        out = await reg.get("facts").handler("")
        assert "No facts" in out

    async def test_add_requires_text(self, svc):
        reg = make_reg(svc)
        out = await reg.get("facts").handler("add")
        assert "用法" in out


class TestAdminFactsCommands:
    async def test_list_and_delete(self, tmp_path, monkeypatch, capsys):
        import types

        from trove.cli import admin_cmds
        from trove.services.user_facts.service import UserFactsService

        svc = UserFactsService(tmp_path / "user_facts.db")
        row = await svc.add("2", "demo", "fact for bob")

        def fake_load_facts():
            return svc

        monkeypatch.setattr(admin_cmds, "_load_facts", fake_load_facts)

        args = types.SimpleNamespace(datasource=None, user=None)
        await admin_cmds._cmd_facts_list(args)
        out = capsys.readouterr().out
        assert "fact for bob" in out
        assert "user=2" in out

        args = types.SimpleNamespace(fact_id=row["id"])
        await admin_cmds._cmd_facts_delete(args)
        assert "Deleted fact" in capsys.readouterr().out
        assert await svc.get("2", row["id"]) is None
