"""``trove extensions`` CLI 冒烟:信封读取面 + 资产包导出/导入(零 LLM / 零网络)。

tmp 项目树 + monkeypatch.chdir —— 项目根 = cwd(与 ``trove preset`` /
``trove validate`` 同一条约定),不碰真实 ``.trove``。「空项目 ≠ 空信封」
也在这里钉一次:code skills 与内置 preset 随包存在。

包面钉「导入之后什么都还没生效」:退出码 0 干净 · 1 有拒载(冲突 / 无效,
或包级失败:被篡改、缺文件、版本过新)· 2 usage。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

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

SKILL_TEXT = (
    "---\n"
    "name: loop-x\n"
    "description: Loop discipline.\n"
    "triggers:\n  node: gen_sql\n"
    "tier: required\n"
    "status: pending\n"
    "source: admin\n"
    "---\n"
    "\n"
    "1. Step one.\n"
    "2. Step two.\n"
)

PRESET = {"name": "starter", "description": "接入模板"}


def _pack_project(tmp_path: Path, name: str, monkeypatch=None) -> Path:
    # 名字带 pack 前缀:E1 的 ``_project``(信封用例)同文件并存,重名会被
    # 后者定义覆盖 —— 并集文件里同名 helper 是最典型的合流陷阱。
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    if monkeypatch is not None:
        monkeypatch.chdir(root)
    return root


def _write_skill(root: Path, name: str = "loop-x", text: str = SKILL_TEXT) -> None:
    d = root / ".trove" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(text, encoding="utf-8")


def _write_preset(root: Path, name: str = "starter", data: dict = PRESET) -> None:
    p = root / ".trove" / "presets" / name / "preset.yml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                 encoding="utf-8")


def _make_pack(tmp_path: Path, monkeypatch, name: str = "demo-pack") -> Path:
    """在 tmp 里造一个包(用服务导出一次 —— CLI 测试只管入口层)。"""
    src = _pack_project(tmp_path, "src", monkeypatch)
    _write_skill(src)
    _write_preset(src)
    dest = tmp_path / "pack"
    assert main_extensions(["export", name, str(dest)]) == 0
    return dest


# ── 导出 ─────────────────────────────────────────────────


def test_export_writes_pack_and_prints_report(tmp_path, monkeypatch, capsys):
    root = _pack_project(tmp_path, "proj", monkeypatch)
    _write_skill(root)
    _write_preset(root)

    assert main_extensions(["export", "demo-pack", str(tmp_path / "pack")]) == 0
    out = capsys.readouterr().out
    assert "导出扩展包 'demo-pack'" in out
    assert "skills/loop-x" in out
    manifest = yaml.safe_load(
        (tmp_path / "pack" / "manifest.yml").read_text(encoding="utf-8"))
    assert manifest["pack_schema"] == 1
    assert manifest["name"] == "demo-pack"
    assert manifest["provenance"]["origin"] == "local"
    assert {f["path"] for f in manifest["files"]} == {
        "skills/loop-x/SKILL.md", "presets/starter/preset.yml"}


def test_export_json_output(tmp_path, monkeypatch, capsys):
    root = _pack_project(tmp_path, "proj", monkeypatch)
    _write_skill(root)
    assert main_extensions(
        ["export", "demo-pack", str(tmp_path / "pack"), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["action"] == "export"
    assert data["counts"]["exported"] == 1
    assert data["items"][0]["item"] == "loop-x"


def test_export_origin_flag(tmp_path, monkeypatch):
    root = _pack_project(tmp_path, "proj", monkeypatch)
    _write_skill(root)
    assert main_extensions(["export", "demo-pack", str(tmp_path / "pack"),
                            "--origin", "export:upstream-pack"]) == 0
    manifest = yaml.safe_load(
        (tmp_path / "pack" / "manifest.yml").read_text(encoding="utf-8"))
    assert manifest["provenance"]["origin"] == "export:upstream-pack"


def test_export_empty_project_fails(tmp_path, monkeypatch, capsys):
    _pack_project(tmp_path, "proj", monkeypatch)
    assert main_extensions(["export", "demo-pack", str(tmp_path / "pack")]) == 1
    err = capsys.readouterr().err
    assert "导出失败" in err and "空" in err
    assert not (tmp_path / "pack").exists()


def test_export_refuses_nonempty_dest(tmp_path, monkeypatch, capsys):
    root = _pack_project(tmp_path, "proj", monkeypatch)
    _write_skill(root)
    dest = tmp_path / "pack"
    dest.mkdir()
    (dest / "keep.txt").write_text("keep", encoding="utf-8")
    assert main_extensions(["export", "demo-pack", str(dest)]) == 1
    assert "非空" in capsys.readouterr().err
    assert (dest / "keep.txt").read_text(encoding="utf-8") == "keep"


# ── 导入:全部 pending ───────────────────────────────────


def test_import_lands_pending_and_exits_clean(tmp_path, monkeypatch, capsys):
    pack = _make_pack(tmp_path, monkeypatch)
    dst = _pack_project(tmp_path, "dst", monkeypatch)

    assert main_extensions(["import", str(pack)]) == 0
    out = capsys.readouterr().out
    assert "逐条确认后才生效" in out
    from trove.services.skills.service import SkillService

    skills = SkillService(dst / ".trove" / "skills", git_enabled=False)
    entry = skills.read_skill("loop-x")
    assert entry["status"] == "pending"
    assert skills.list_org(confirmed_only=True) == []
    assert (dst / ".trove" / "presets" / "starter" / "preset.yml").is_file()


def test_import_json_output(tmp_path, monkeypatch, capsys):
    pack = _make_pack(tmp_path, monkeypatch)
    capsys.readouterr()                      # 丢掉 export 那次的输出
    _pack_project(tmp_path, "dst", monkeypatch)
    assert main_extensions(["import", str(pack), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["action"] == "import"
    assert data["counts"]["imported"] == 2
    assert data["origin"] == "local"


def test_import_tampered_pack_fails_and_lands_nothing(tmp_path, monkeypatch,
                                                     capsys):
    pack = _make_pack(tmp_path, monkeypatch)
    (pack / "files" / "skills" / "loop-x" / "SKILL.md").write_bytes(b"tampered")
    dst = _pack_project(tmp_path, "dst", monkeypatch)

    assert main_extensions(["import", str(pack)]) == 1
    err = capsys.readouterr().err
    assert "未落任何资产" in err
    assert "skills/loop-x/SKILL.md" in err
    assert not (dst / ".trove" / "skills").exists()


def test_import_newer_pack_schema_fails_and_lands_nothing(tmp_path, monkeypatch,
                                                          capsys):
    pack = _make_pack(tmp_path, monkeypatch)
    path = pack / "manifest.yml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["pack_schema"] = 2
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                    encoding="utf-8")
    dst = _pack_project(tmp_path, "dst", monkeypatch)

    assert main_extensions(["import", str(pack)]) == 1
    assert "pack_schema" in capsys.readouterr().err
    assert not (dst / ".trove" / "skills").exists()


def test_import_not_a_pack_fails(tmp_path, monkeypatch, capsys):
    _pack_project(tmp_path, "dst", monkeypatch)
    assert main_extensions(["import", str(tmp_path / "nothing")]) == 1
    assert "manifest.yml" in capsys.readouterr().err


def test_import_conflict_exits_1_but_lands_the_rest(tmp_path, monkeypatch,
                                                    capsys):
    pack = _make_pack(tmp_path, monkeypatch)
    dst = _pack_project(tmp_path, "dst", monkeypatch)
    _write_skill(dst, text=SKILL_TEXT.replace("Step one", "a different body"))
    before = (dst / ".trove" / "skills" / "loop-x" / "SKILL.md").read_bytes()

    assert main_extensions(["import", str(pack)]) == 1
    out = capsys.readouterr().out
    assert "冲突拒载" in out and "同名 org skill 已存在" in out
    assert (dst / ".trove" / "skills" / "loop-x" / "SKILL.md").read_bytes() \
        == before
    assert (dst / ".trove" / "presets" / "starter" / "preset.yml").is_file()


def test_import_force_overwrites_cleanly(tmp_path, monkeypatch):
    pack = _make_pack(tmp_path, monkeypatch)
    dst = _pack_project(tmp_path, "dst", monkeypatch)
    _write_skill(dst, text=SKILL_TEXT.replace("Step one", "old body"))

    assert main_extensions(["import", str(pack), "--force"]) == 0
    assert (dst / ".trove" / "skills" / "loop-x" / "SKILL.md").read_text(
        encoding="utf-8") == SKILL_TEXT
    from trove.services.skills.service import SkillService

    entry = SkillService(dst / ".trove" / "skills",
                         git_enabled=False).read_skill("loop-x")
    assert entry["status"] == "pending"


def test_import_is_idempotent_on_rerun(tmp_path, monkeypatch, capsys):
    pack = _make_pack(tmp_path, monkeypatch)
    _pack_project(tmp_path, "dst", monkeypatch)
    assert main_extensions(["import", str(pack)]) == 0
    capsys.readouterr()
    assert main_extensions(["import", str(pack)]) == 0
    assert "跳过" in capsys.readouterr().out


# ── 往返(cli 级) ────────────────────────────────────────


def test_cli_roundtrip_preserves_bytes(tmp_path, monkeypatch):
    src = _pack_project(tmp_path, "src", monkeypatch)
    _write_skill(src)
    _write_preset(src)
    pack = tmp_path / "pack"
    assert main_extensions(["export", "demo-pack", str(pack)]) == 0

    dst = _pack_project(tmp_path, "dst", monkeypatch)
    assert main_extensions(["import", str(pack)]) == 0
    assert ((dst / ".trove" / "skills" / "loop-x" / "SKILL.md").read_bytes()
            == (src / ".trove" / "skills" / "loop-x" / "SKILL.md").read_bytes())
    assert ((dst / ".trove" / "presets" / "starter" / "preset.yml").read_bytes()
            == (src / ".trove" / "presets" / "starter" / "preset.yml").read_bytes())


# ── usage ────────────────────────────────────────────────


@pytest.mark.parametrize("argv", [[], ["export"], ["export", "demo-pack"],
                                  ["import"], ["frobnicate"]])
def test_usage_errors_exit_2(tmp_path, monkeypatch, argv):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as ei:
        main_extensions(argv)
    assert ei.value.code == 2
