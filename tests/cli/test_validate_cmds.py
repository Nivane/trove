"""``trove validate`` CLI 冒烟:退出码就是结论(CI 直接挂它)。

tmp 项目树 + monkeypatch.chdir —— 命令的项目根 = cwd(与 REPL 里
SkillService(Path.cwd()/.trove/skills) 同一条约定),不碰真实 .trove。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from trove.cli.commands.validate_cmds import main_validate, register_validate_commands

_SEMANTICS = {
    "semantic_model": [{
        "name": "mini",
        "datasets": [{
            "name": "loan",
            "source": "loan",
            "primary_key": ["loan_id"],
            "fields": [
                {"name": "loan_id", "datatype": "Integer",
                 "description": "Loan id.",
                 "expression": {"dialects": [
                     {"dialect": "ANSI_SQL", "expression": "loan_id"}]}},
                {"name": "date", "datatype": "Date",
                 "description": "Booking date.",
                 "expression": {"dialects": [
                     {"dialect": "ANSI_SQL", "expression": "date"}]}},
            ],
        }],
        "metrics": [{
            "name": "loan_count",
            "agg_time_dimension": "date",
            "expression": {"dialects": [
                {"dialect": "ANSI_SQL", "expression": "COUNT(loan.loan_id)"}]},
        }],
    }],
}

_SCHEMA_NOTES = {
    "tables": [{
        "name": "loan",
        "description": "Loans.",
        "columns": [
            {"name": "loan_id", "description": "Loan id."},
            {"name": "date", "description": "Booking date."},
        ],
    }],
}


def _project(tmp_path: Path, monkeypatch, *, semantics: bool = True,
             examples: dict | None = None) -> Path:
    monkeypatch.chdir(tmp_path)
    ds_dir = tmp_path / ".trove" / "kb" / "mini"
    ds_dir.mkdir(parents=True)
    (ds_dir / "schema_notes.yml").write_text(
        yaml.safe_dump(_SCHEMA_NOTES, allow_unicode=True), encoding="utf-8")
    if semantics:
        (ds_dir / "semantics.yml").write_text(
            yaml.safe_dump(_SEMANTICS, allow_unicode=True), encoding="utf-8")
    if examples:
        (ds_dir / "examples.yml").write_text(
            yaml.safe_dump(examples, allow_unicode=True), encoding="utf-8")
    return ds_dir


def test_cli_clean_exit_0(tmp_path, monkeypatch, capsys):
    _project(tmp_path, monkeypatch)
    assert main_validate(["--datasource", "mini"]) == 0
    assert "结论:" in capsys.readouterr().out


def test_cli_json_is_machine_readable(tmp_path, monkeypatch, capsys):
    import json

    _project(tmp_path, monkeypatch)
    assert main_validate(["--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True and data["datasources"] == ["mini"]


def test_cli_hard_error_exit_1(tmp_path, monkeypatch, capsys):
    _project(tmp_path, monkeypatch, semantics=False)
    assert main_validate(["--datasource", "mini"]) == 1
    assert "semantics.yml" in capsys.readouterr().out


def test_cli_strict_promotes_warnings(tmp_path, monkeypatch, capsys):
    """警告默认不拦;--strict 才拦 —— 两种口径都断一次。"""
    _project(tmp_path, monkeypatch, examples={"examples": [
        {"question": "How many loans?", "sql": "SELECT COUNT(*) FROM loan"},
        {"question": "How many loans?", "sql": "SELECT COUNT(loan_id) FROM loan"},
    ]})
    assert main_validate(["--datasource", "mini"]) == 0
    assert main_validate(["--datasource", "mini", "--strict"]) == 1
    assert "同一问题" in capsys.readouterr().out


def test_cli_unknown_datasource_exit_1(tmp_path, monkeypatch, capsys):
    _project(tmp_path, monkeypatch)
    assert main_validate(["--datasource", "ghost"]) == 1
    assert "KB 目录不存在" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_repl_slash_command_runs(tmp_path, monkeypatch):
    """REPL 的 /validate 走同一份报告(无退出码,返回可读文本)。"""
    from trove.cli.slash_registry import SlashRegistry

    _project(tmp_path, monkeypatch)
    registry = SlashRegistry()
    register_validate_commands(registry, {"connector_registry": None})
    cmd = registry.get("validate")
    assert cmd is not None
    out = await cmd.handler("")
    assert "trove validate" in out and "结论:" in out
