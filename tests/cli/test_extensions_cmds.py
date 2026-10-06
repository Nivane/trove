"""``trove extensions`` CLI 冒烟 —— 信封读取面(只读,零 LLM / 零网络)。

tmp 项目树 + monkeypatch.chdir:命令的项目根 = cwd(与 validate 同一约定)。
「空项目 ≠ 空信封」也在这里钉一次:code skills 与内置 preset 随包存在。
"""

from __future__ import annotations

import json
from pathlib import Path

from trove.cli.commands.extensions_cmds import (
    main_extensions,
    register_extensions_commands,
)

_ORG_SKILL = (
    "---\nname: no-neg\ndescription: No negative values.\n"
    "tier: validator\nstatus: confirmed\ntargets: [result]\n"
    "triggers: {node: validate}\n"
    "checks:\n  - expr: min >= 0\n    severity: blocking\n---\n\n正文\n"
)


def _project(tmp_path: Path, monkeypatch, *, with_skill: bool = True) -> None:
    monkeypatch.chdir(tmp_path)
    if with_skill:
        d = tmp_path / ".trove" / "skills" / "no-neg"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(_ORG_SKILL, encoding="utf-8")


def test_list_shows_org_skill_and_code_assets(tmp_path, monkeypatch, capsys):
    _project(tmp_path, monkeypatch)
    assert main_extensions(["list"]) == 0
    out = capsys.readouterr().out
    assert "no-neg" in out and "org" in out
    assert "plan_query" in out          # code skill 随包出现


def test_list_json_shape(tmp_path, monkeypatch, capsys):
    _project(tmp_path, monkeypatch)
    assert main_extensions(["list", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert set(data) == {"extensions"}
    for e in data["extensions"]:
        assert set(e) == {"kind", "name", "source", "state", "mounts",
                          "capabilities", "provenance", "unresolved"}
    org = next(e for e in data["extensions"] if e["name"] == "no-neg")
    assert org["mounts"][0]["tier"] == "validator"
    assert org["capabilities"]["variables"] == ["min"]
    assert org["capabilities"]["effects"] == ["takeover"]


def test_list_kind_filter(tmp_path, monkeypatch, capsys):
    _project(tmp_path, monkeypatch)
    assert main_extensions(["list", "--kind", "preset", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["extensions"]
    assert {e["kind"] for e in data["extensions"]} == {"preset"}


def test_list_empty_kind_prints_placeholder(tmp_path, monkeypatch, capsys):
    _project(tmp_path, monkeypatch, with_skill=False)
    assert main_extensions(["list", "--kind", "term-set"]) == 0
    assert "没有扩展资产" in capsys.readouterr().out


def test_show_hit(tmp_path, monkeypatch, capsys):
    _project(tmp_path, monkeypatch)
    assert main_extensions(["show", "no-neg"]) == 0
    out = capsys.readouterr().out
    assert "[skill] no-neg" in out
    assert "挂点" in out and "能力(推导)" in out and "来源: org" in out


def test_show_missing_exit_1(tmp_path, monkeypatch, capsys):
    _project(tmp_path, monkeypatch)
    assert main_extensions(["show", "ghost"]) == 1
    assert "未找到资产" in capsys.readouterr().err


def test_show_kind_mismatch_exit_1(tmp_path, monkeypatch, capsys):
    _project(tmp_path, monkeypatch)
    assert main_extensions(["show", "no-neg", "--kind", "decision"]) == 1
    capsys.readouterr()


def test_show_json(tmp_path, monkeypatch, capsys):
    _project(tmp_path, monkeypatch)
    assert main_extensions(["show", "no-neg", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["extensions"][0]["name"] == "no-neg"


async def test_repl_slash_command_runs(tmp_path, monkeypatch):
    """REPL 的 /extensions 走同一份信封(无退出码,返回可读文本)。"""
    from trove.cli.slash_registry import SlashRegistry

    _project(tmp_path, monkeypatch)
    registry = SlashRegistry()
    register_extensions_commands(registry, {})
    cmd = registry.get("extensions")
    assert cmd is not None
    out = await cmd.handler("list")
    assert "no-neg" in out
    out = await cmd.handler("show no-neg")
    assert "挂点" in out
    out = await cmd.handler("show ghost")
    assert "未找到资产" in out
