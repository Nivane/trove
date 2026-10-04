"""``trove preset`` CLI 冒烟:退出码 +「套用之后什么都还没生效」。

tmp 项目树 + monkeypatch.chdir —— 项目根 = cwd(与 ``trove validate`` /
REPL 同一条约定),不碰真实 ``.trove``。内置 preset 目录来自代码库自身
(``trove/presets/``),所以这里套的是**随码分发的那份示例**。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from trove.cli.commands.preset_cmds import main_preset, register_preset_commands

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


def _project(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.chdir(tmp_path)
    ds_dir = tmp_path / ".trove" / "kb" / "mini"
    ds_dir.mkdir(parents=True)
    (ds_dir / "semantics.yml").write_text(
        yaml.safe_dump(_SEMANTICS, allow_unicode=True), encoding="utf-8")
    return ds_dir


def test_cli_list_shows_builtin(tmp_path, monkeypatch, capsys):
    _project(tmp_path, monkeypatch)
    assert main_preset(["list"]) == 0
    out = capsys.readouterr().out
    assert "financial-analysis" in out and "[builtin]" in out


def test_cli_list_json_is_machine_readable(tmp_path, monkeypatch, capsys):
    import json

    _project(tmp_path, monkeypatch)
    assert main_preset(["list", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    rows = {r["name"]: r for r in data["presets"]}
    assert rows["financial-analysis"]["source"] == "builtin"


def test_cli_show_prints_contract_and_text(tmp_path, monkeypatch, capsys):
    _project(tmp_path, monkeypatch)
    assert main_preset(["show", "financial-analysis"]) == 0
    out = capsys.readouterr().out
    assert "条目:" in out and "name: financial-analysis" in out


def test_cli_show_unknown_exit_1(tmp_path, monkeypatch, capsys):
    _project(tmp_path, monkeypatch)
    assert main_preset(["show", "nope"]) == 1
    assert "nope" in capsys.readouterr().err


def test_cli_apply_lands_pending_only(tmp_path, monkeypatch, capsys):
    """回归门(CLI 面):套用打印报告,但生效面一个字节都没动。"""
    ds_dir = _project(tmp_path, monkeypatch)
    assert main_preset(
        ["apply", "financial-analysis", "--datasource", "mini"]) == 0
    out = capsys.readouterr().out
    assert "period-comparison" in out and "草稿" in out

    # 技能:落的是 org skill 的 pending 草稿(用真实读路径判,不手解 frontmatter)
    from trove.services.skills.service import SkillService

    skill_md = tmp_path / ".trove" / "skills" / "period-comparison" / "SKILL.md"
    assert skill_md.exists()
    entry = SkillService(tmp_path / ".trove" / "skills").read_skill(
        "period-comparison")
    assert entry is not None and entry["status"] == "pending"

    # 规则:在 decision_drafts.yml 里,decisions.yml(唯一执行读源)不存在
    assert not (ds_dir / "decisions.yml").exists()
    drafts = yaml.safe_load(
        (ds_dir / "decision_drafts.yml").read_text(encoding="utf-8"))["drafts"]
    assert {d["rule"]["id"] for d in drafts} == {
        "watch-bad-debt-ratio", "watch-amount-by-region"}
    assert all(d["status"] == "pending" for d in drafts)
    assert all(d["rule"]["enabled"] is False for d in drafts)   # 安全默认


def test_cli_apply_strict_flags_unresolved(tmp_path, monkeypatch, capsys):
    """内置示例的域骨架 datasets 为空 → unresolved;默认只报告,--strict 拦。"""
    _project(tmp_path, monkeypatch)
    assert main_preset(
        ["apply", "financial-analysis", "--datasource", "mini"]) == 0
    capsys.readouterr()
    assert main_preset(
        ["apply", "financial-analysis", "--datasource", "mini",
         "--strict"]) == 1


def test_cli_apply_unknown_exit_1(tmp_path, monkeypatch, capsys):
    _project(tmp_path, monkeypatch)
    assert main_preset(["apply", "nope", "--datasource", "mini"]) == 1
    assert "nope" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_repl_slash_preset_runs(tmp_path, monkeypatch):
    """/preset 走同一份服务:list 可读;apply 用默认数据源。"""
    from trove.cli.slash_registry import SlashRegistry

    _project(tmp_path, monkeypatch)
    registry = SlashRegistry()
    register_preset_commands(
        registry, {"connector_registry": type("R", (), {"default_name": "mini"})()})
    cmd = registry.get("preset")
    assert cmd is not None

    listed = await cmd.handler("list")
    assert "financial-analysis" in listed

    report = await cmd.handler("apply financial-analysis")
    assert "period-comparison" in report
    assert (tmp_path / ".trove" / "skills" / "period-comparison"
            / "SKILL.md").exists()
    # 没选数据源时给的是用法提示,不是异常
    empty = SlashRegistry()
    register_preset_commands(empty, {"connector_registry": None})
    out = await empty.get("preset").handler("apply financial-analysis")
    assert "默认数据源" in out
