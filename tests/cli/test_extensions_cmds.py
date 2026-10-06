"""``trove extensions`` CLI 冒烟:退出码 + 报告 +「导入之后什么都还没生效」。

tmp 项目树 + monkeypatch.chdir —— 项目根 = cwd(与 ``trove preset`` /
``trove validate`` 同一条约定),不碰真实 ``.trove``。

退出码约定:0 干净 · 1 有拒载(冲突 / 无效,或包级失败:被篡改、缺文件、
版本过新)· 2 usage。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from trove.cli.commands.extensions_cmds import main_extensions

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


def _project(tmp_path: Path, name: str, monkeypatch=None) -> Path:
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
    src = _project(tmp_path, "src", monkeypatch)
    _write_skill(src)
    _write_preset(src)
    dest = tmp_path / "pack"
    assert main_extensions(["export", name, str(dest)]) == 0
    return dest


# ── 导出 ─────────────────────────────────────────────────


def test_export_writes_pack_and_prints_report(tmp_path, monkeypatch, capsys):
    root = _project(tmp_path, "proj", monkeypatch)
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
    root = _project(tmp_path, "proj", monkeypatch)
    _write_skill(root)
    assert main_extensions(
        ["export", "demo-pack", str(tmp_path / "pack"), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["action"] == "export"
    assert data["counts"]["exported"] == 1
    assert data["items"][0]["item"] == "loop-x"


def test_export_origin_flag(tmp_path, monkeypatch):
    root = _project(tmp_path, "proj", monkeypatch)
    _write_skill(root)
    assert main_extensions(["export", "demo-pack", str(tmp_path / "pack"),
                            "--origin", "export:upstream-pack"]) == 0
    manifest = yaml.safe_load(
        (tmp_path / "pack" / "manifest.yml").read_text(encoding="utf-8"))
    assert manifest["provenance"]["origin"] == "export:upstream-pack"


def test_export_empty_project_fails(tmp_path, monkeypatch, capsys):
    _project(tmp_path, "proj", monkeypatch)
    assert main_extensions(["export", "demo-pack", str(tmp_path / "pack")]) == 1
    err = capsys.readouterr().err
    assert "导出失败" in err and "空" in err
    assert not (tmp_path / "pack").exists()


def test_export_refuses_nonempty_dest(tmp_path, monkeypatch, capsys):
    root = _project(tmp_path, "proj", monkeypatch)
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
    dst = _project(tmp_path, "dst", monkeypatch)

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
    _project(tmp_path, "dst", monkeypatch)
    assert main_extensions(["import", str(pack), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["action"] == "import"
    assert data["counts"]["imported"] == 2
    assert data["origin"] == "local"


def test_import_tampered_pack_fails_and_lands_nothing(tmp_path, monkeypatch,
                                                     capsys):
    pack = _make_pack(tmp_path, monkeypatch)
    (pack / "files" / "skills" / "loop-x" / "SKILL.md").write_bytes(b"tampered")
    dst = _project(tmp_path, "dst", monkeypatch)

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
    dst = _project(tmp_path, "dst", monkeypatch)

    assert main_extensions(["import", str(pack)]) == 1
    assert "pack_schema" in capsys.readouterr().err
    assert not (dst / ".trove" / "skills").exists()


def test_import_not_a_pack_fails(tmp_path, monkeypatch, capsys):
    _project(tmp_path, "dst", monkeypatch)
    assert main_extensions(["import", str(tmp_path / "nothing")]) == 1
    assert "manifest.yml" in capsys.readouterr().err


def test_import_conflict_exits_1_but_lands_the_rest(tmp_path, monkeypatch,
                                                    capsys):
    pack = _make_pack(tmp_path, monkeypatch)
    dst = _project(tmp_path, "dst", monkeypatch)
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
    dst = _project(tmp_path, "dst", monkeypatch)
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
    _project(tmp_path, "dst", monkeypatch)
    assert main_extensions(["import", str(pack)]) == 0
    capsys.readouterr()
    assert main_extensions(["import", str(pack)]) == 0
    assert "跳过" in capsys.readouterr().out


# ── 往返(cli 级) ────────────────────────────────────────


def test_cli_roundtrip_preserves_bytes(tmp_path, monkeypatch):
    src = _project(tmp_path, "src", monkeypatch)
    _write_skill(src)
    _write_preset(src)
    pack = tmp_path / "pack"
    assert main_extensions(["export", "demo-pack", str(pack)]) == 0

    dst = _project(tmp_path, "dst", monkeypatch)
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
