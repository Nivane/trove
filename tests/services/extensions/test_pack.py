"""扩展资产包(``extensions/pack.py``)—— 清单契约、往返保真、拒载姿势。

这个文件盯住三件事(实施稿 §07 验收):

1. **往返保真**:写出去的包读回来,逐文件字节相同、sha256 全等;
2. **篡改拒载并点名** —— 改一字节 / 删一个文件,报错里必须看得见**哪个**
   文件(而非"校验失败"四个字);
3. **版本门**:``pack_schema`` 高于代码已知版本 → 拒载(照
   ``StorageSchemaTooNew`` 的精神),且是**读之前**就拒 —— 看不懂的清单
   不做任何解释。

零 LLM / 零网络:全部在 tmp 目录里读写文件。
"""

from __future__ import annotations

import shutil
import subprocess

import pytest
import yaml

from trove.services.extensions import pack as pack_mod
from trove.services.extensions.pack import (
    FILES_DIR,
    MANIFEST_FILE,
    MANIFEST_KEYS,
    PACK_SCHEMA,
    PACK_STATUSES,
    LoadedPack,
    PackError,
    PackInvalid,
    PackManifest,
    PackReport,
    PackSchemaTooNew,
    PackTampered,
    PackedFile,
    is_valid_relpath,
    kind_of,
    read_pack,
    sha256_hex,
    write_pack,
)

GOOD_SHA = sha256_hex(b"select 1\n")


def _write_raw_pack(root, files: dict[str, bytes], **over):
    """手写一个包目录(绕过 write_pack 的守卫)—— 构造坏清单用。"""
    root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "pack_schema": PACK_SCHEMA,
        "name": "demo-pack",
        "exported_at": "2026-01-01T00:00:00+00:00",
        "tool_version": "9.9.9",
        "kinds": sorted({k for k in (kind_of(p) for p in files) if k}),
        "files": [{"path": p, "sha256": sha256_hex(b)} for p, b in files.items()],
        "provenance": {"origin": "local", "git_rev": "abc1234"},
    }
    manifest.update(over)
    (root / MANIFEST_FILE).write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    for rel, blob in files.items():
        target = root / FILES_DIR / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)
    return root


def _rewrite_manifest(root, mutate) -> None:
    """读出现有 manifest → ``mutate(dict)`` → 写回(构造版本门 / 坏键用)。"""
    path = root / MANIFEST_FILE
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                    encoding="utf-8")


# ── 清单契约(§3j) ───────────────────────────────────────


def test_manifest_keys_are_the_frozen_contract():
    """清单是闭键集:七个键,一个不多一个不少(§3j 冻死的形状)。"""
    assert set(MANIFEST_KEYS) == {
        "pack_schema", "name", "exported_at", "tool_version", "kinds",
        "files", "provenance",
    }


def test_manifest_to_dict_key_order_matches_spec(tmp_path):
    """写盘键序 = §3j 的顺序(人读的 diff 要对齐验收稿)。"""
    manifest = PackManifest(
        name="demo-pack",
        files=(PackedFile(path="skills/x/SKILL.md", sha256=GOOD_SHA),),
        exported_at="2026-01-01T00:00:00+00:00", tool_version="1.0",
        origin="local", git_rev="deadbee")
    assert list(manifest.to_dict()) == [
        "pack_schema", "name", "exported_at", "tool_version", "kinds",
        "files", "provenance",
    ]
    assert list(manifest.to_dict()["files"][0]) == ["path", "sha256"]
    assert manifest.kinds == ("skills",)


def test_write_pack_layout_and_hashes(tmp_path):
    """导出布局 = ``manifest.yml`` + ``files/`` 按原相对路径镜像,sha256 对得上。"""
    dest = tmp_path / "pack"
    files = {
        "skills/loop-x/SKILL.md": b"---\nname: loop-x\n---\n\nbody\n",
        "kb/mini/decisions.yml": b"version: 1\nrules: []\n",
    }
    manifest = write_pack(dest, name="demo-pack", files=files,
                          origin="export:upstream", git_rev="abc1234",
                          tool_version="1.2.3",
                          exported_at="2026-01-01T00:00:00+00:00")
    assert (dest / MANIFEST_FILE).is_file()
    for rel, blob in files.items():
        assert (dest / FILES_DIR / rel).read_bytes() == blob
    parsed = yaml.safe_load((dest / MANIFEST_FILE).read_text(encoding="utf-8"))
    assert parsed["pack_schema"] == PACK_SCHEMA
    assert parsed["kinds"] == ["decisions", "skills"]
    assert parsed["provenance"] == {"origin": "export:upstream",
                                    "git_rev": "abc1234"}
    assert {f["path"]: f["sha256"] for f in parsed["files"]} == {
        rel: sha256_hex(blob) for rel, blob in files.items()}
    assert manifest.name == "demo-pack"


@pytest.mark.parametrize("origin", ["local", "export:upstream-pack",
                                    "export:pack.v2"])
def test_write_pack_accepts_legal_origins(tmp_path, origin):
    write_pack(tmp_path / "pack", name="demo-pack",
               files={"skills/x/SKILL.md": b"x"}, origin=origin)
    assert read_pack(tmp_path / "pack").manifest.origin == origin


# ── 写入面的守卫 ─────────────────────────────────────────


def test_write_pack_rejects_empty_files(tmp_path):
    """空包拒写 —— "成功导出了一个什么都没有的包"比失败更糟。"""
    with pytest.raises(PackError, match="空"):
        write_pack(tmp_path / "pack", name="demo-pack", files={})


def test_write_pack_rejects_bad_name(tmp_path):
    with pytest.raises(PackError, match="包名"):
        write_pack(tmp_path / "pack", name="Bad Name!",
                   files={"skills/x/SKILL.md": b"x"})


def test_write_pack_rejects_bad_origin(tmp_path):
    with pytest.raises(PackError, match="origin"):
        write_pack(tmp_path / "pack", name="demo-pack",
                   files={"skills/x/SKILL.md": b"x"},
                   origin="somewhere-else")


def test_write_pack_rejects_nonempty_dest(tmp_path):
    """目标非空拒写:导出不该悄悄覆盖一份已有的包(或任何目录)。"""
    dest = tmp_path / "pack"
    dest.mkdir()
    (dest / "stray.txt").write_text("keep me", encoding="utf-8")
    with pytest.raises(PackError, match="非空"):
        write_pack(dest, name="demo-pack", files={"skills/x/SKILL.md": b"x"})
    assert (dest / "stray.txt").read_text(encoding="utf-8") == "keep me"


def test_write_pack_rejects_dest_file(tmp_path):
    target = tmp_path / "pack"
    target.write_text("i am a file", encoding="utf-8")
    with pytest.raises(PackError, match="不是目录"):
        write_pack(target, name="demo-pack", files={"skills/x/SKILL.md": b"x"})


@pytest.mark.parametrize("rel", [
    "../escape.md", "/abs.md", "a/../../b.md", "a\\b.md", "a//b.md", "",
    "a/./b.md",
])
def test_write_pack_rejects_unsafe_relpath(tmp_path, rel):
    """包的写入面不得越出包根(路径从包的输入来,不能信)。"""
    with pytest.raises(PackError, match="路径"):
        write_pack(tmp_path / "pack", name="demo-pack", files={rel: b"x"})


def test_write_pack_rejects_non_bytes(tmp_path):
    with pytest.raises(PackError, match="字节"):
        write_pack(tmp_path / "pack", name="demo-pack",
                   files={"skills/x/SKILL.md": "str, not bytes"})


# ── 往返保真 ─────────────────────────────────────────────


def test_roundtrip_bytes_and_sha256_identical(tmp_path):
    """导出 → 读回:逐文件字节相同,且 sha256 与清单声明全等。"""
    files = {
        "skills/loop-x/SKILL.md": "---\nname: loop-x\n---\n\n中文正文\n".encode(),
        "skills/loop-x/SKILL.zh.md": b"override\n",
        "kb/mini/decisions.yml": b"version: 1\nrules: []\n",
        "presets/starter/preset.yml": b"name: starter\ndescription: d\n",
    }
    manifest = write_pack(tmp_path / "pack", name="demo-pack", files=files)
    loaded = read_pack(tmp_path / "pack")
    assert loaded.files == files
    for entry in manifest.files:
        assert entry.sha256 == sha256_hex(files[entry.path])
    assert loaded.extra == ()


def test_extra_files_are_reported_but_not_imported(tmp_path):
    """手放进 ``files/`` 但不进清单的文件:报出来,不算包的一部分。"""
    write_pack(tmp_path / "pack", name="demo-pack",
               files={"skills/x/SKILL.md": b"x"})
    stray = tmp_path / "pack" / FILES_DIR / "skills" / "sneaky" / "SKILL.md"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"not declared")
    loaded = read_pack(tmp_path / "pack")
    assert loaded.extra == ("skills/sneaky/SKILL.md",)
    assert "skills/sneaky/SKILL.md" not in loaded.files


# ── 篡改:点名到文件 ─────────────────────────────────────


def test_tampered_byte_refuses_and_names_file(tmp_path):
    """改一个字节 → 拒载,且报错里点得出是哪个文件被改。"""
    write_pack(tmp_path / "pack", name="demo-pack", files={
        "skills/loop-x/SKILL.md": b"body one\n",
        "kb/mini/decisions.yml": b"rules: []\n",
    })
    target = tmp_path / "pack" / FILES_DIR / "skills" / "loop-x" / "SKILL.md"
    target.write_bytes(b"body two\n")
    with pytest.raises(PackTampered) as ei:
        read_pack(tmp_path / "pack")
    assert ei.value.modified == ("skills/loop-x/SKILL.md",)
    assert ei.value.missing == ()
    assert "skills/loop-x/SKILL.md" in str(ei.value)


def test_missing_file_refuses_and_names_file(tmp_path):
    write_pack(tmp_path / "pack", name="demo-pack", files={
        "skills/loop-x/SKILL.md": b"x",
        "kb/mini/decisions.yml": b"rules: []\n",
    })
    (tmp_path / "pack" / FILES_DIR / "kb" / "mini" / "decisions.yml").unlink()
    with pytest.raises(PackTampered) as ei:
        read_pack(tmp_path / "pack")
    assert ei.value.missing == ("kb/mini/decisions.yml",)
    assert ei.value.modified == ()


def test_tamper_names_every_offending_file(tmp_path):
    """多文件被改:一次报全(而不是修一个再发现下一个)。"""
    write_pack(tmp_path / "pack", name="demo-pack", files={
        "skills/a/SKILL.md": b"a", "skills/b/SKILL.md": b"b",
    })
    for name in ("a", "b"):
        p = tmp_path / "pack" / FILES_DIR / "skills" / name / "SKILL.md"
        p.write_bytes(b"changed")
    with pytest.raises(PackTampered) as ei:
        read_pack(tmp_path / "pack")
    assert set(ei.value.modified) == {"skills/a/SKILL.md", "skills/b/SKILL.md"}


# ── 版本门 ───────────────────────────────────────────────


def test_pack_schema_two_refuses(tmp_path):
    """``pack_schema: 2`` → 拒载(旧代码读新包 = 静默丢资产,不能发生)。"""
    _write_raw_pack(tmp_path / "pack", {"skills/x/SKILL.md": b"x"})
    _rewrite_manifest(tmp_path / "pack", lambda d: d.update(pack_schema=2))
    with pytest.raises(PackSchemaTooNew) as ei:
        read_pack(tmp_path / "pack")
    assert ei.value.manifest_version == 2
    assert ei.value.supported == PACK_SCHEMA
    assert "升级" in str(ei.value)


def test_version_gate_fires_before_file_verification(tmp_path):
    """版本门在读文件**之前**:看不懂的清单不该先被当作可用清单核对一遍。"""
    _write_raw_pack(tmp_path / "pack", {"skills/x/SKILL.md": b"x"})
    _rewrite_manifest(tmp_path / "pack", lambda d: d.update(pack_schema=99))
    (tmp_path / "pack" / FILES_DIR / "skills" / "x" / "SKILL.md").write_bytes(
        b"tampered too")
    with pytest.raises(PackSchemaTooNew):
        read_pack(tmp_path / "pack")


@pytest.mark.parametrize("value", [0, -1, "1", 1.5, True, None])
def test_pack_schema_invalid_values_refuse(tmp_path, value):
    _write_raw_pack(tmp_path / "pack", {"skills/x/SKILL.md": b"x"})
    _rewrite_manifest(tmp_path / "pack", lambda d: d.update(pack_schema=value))
    with pytest.raises(PackInvalid, match="pack_schema"):
        read_pack(tmp_path / "pack")


# ── 清单结构拒载 ─────────────────────────────────────────


def test_missing_manifest_refuses(tmp_path):
    root = tmp_path / "not-a-pack"
    root.mkdir()
    with pytest.raises(PackInvalid, match=MANIFEST_FILE):
        read_pack(root)


def test_manifest_unknown_key_lists_legal_keys(tmp_path):
    _write_raw_pack(tmp_path / "pack", {"skills/x/SKILL.md": b"x"})
    _rewrite_manifest(tmp_path / "pack", lambda d: d.update(whatever=1))
    with pytest.raises(PackInvalid) as ei:
        read_pack(tmp_path / "pack")
    assert "whatever" in str(ei.value)
    assert "files" in str(ei.value) and "provenance" in str(ei.value)


@pytest.mark.parametrize("key", sorted(MANIFEST_KEYS))
def test_manifest_missing_required_key_refuses(tmp_path, key):
    _write_raw_pack(tmp_path / "pack", {"skills/x/SKILL.md": b"x"})
    _rewrite_manifest(tmp_path / "pack", lambda d: d.pop(key))
    with pytest.raises(PackInvalid, match="缺少必填键"):
        read_pack(tmp_path / "pack")


def test_manifest_name_must_be_canonical(tmp_path):
    _write_raw_pack(tmp_path / "pack", {"skills/x/SKILL.md": b"x"})
    _rewrite_manifest(tmp_path / "pack", lambda d: d.update(name="Bad Name"))
    with pytest.raises(PackInvalid, match="包名"):
        read_pack(tmp_path / "pack")


def test_provenance_must_be_traceable(tmp_path):
    """来源必须可查:缺 origin / 不合法 / 未知键,一律拒载。"""
    _write_raw_pack(tmp_path / "pack", {"skills/x/SKILL.md": b"x"})
    _rewrite_manifest(tmp_path / "pack",
                      lambda d: d.update(provenance={"origin": "internet"}))
    with pytest.raises(PackInvalid, match="origin"):
        read_pack(tmp_path / "pack")


def test_provenance_unknown_key_refuses(tmp_path):
    _write_raw_pack(tmp_path / "pack", {"skills/x/SKILL.md": b"x"})
    _rewrite_manifest(
        tmp_path / "pack",
        lambda d: d.update(provenance={"origin": "local", "note": "hi"}))
    with pytest.raises(PackInvalid, match="note"):
        read_pack(tmp_path / "pack")


def test_empty_files_list_refuses(tmp_path):
    _write_raw_pack(tmp_path / "pack", {"skills/x/SKILL.md": b"x"})
    _rewrite_manifest(tmp_path / "pack", lambda d: d.update(files=[]))
    with pytest.raises(PackInvalid, match="非空列表"):
        read_pack(tmp_path / "pack")


def test_file_entry_unknown_key_refuses(tmp_path):
    _write_raw_pack(tmp_path / "pack", {"skills/x/SKILL.md": b"x"})
    _rewrite_manifest(
        tmp_path / "pack",
        lambda d: d["files"][0].update(size=3))
    with pytest.raises(PackInvalid, match="size"):
        read_pack(tmp_path / "pack")


@pytest.mark.parametrize("bad_path", ["../x.md", "/x.md", "a\\b.md"])
def test_file_entry_unsafe_path_refuses(tmp_path, bad_path):
    _write_raw_pack(tmp_path / "pack", {"skills/x/SKILL.md": b"x"})
    _rewrite_manifest(
        tmp_path / "pack",
        lambda d: d["files"][0].update(path=bad_path))
    with pytest.raises(PackInvalid, match="安全相对路径"):
        read_pack(tmp_path / "pack")


def test_file_entry_bad_sha_refuses(tmp_path):
    _write_raw_pack(tmp_path / "pack", {"skills/x/SKILL.md": b"x"})
    _rewrite_manifest(
        tmp_path / "pack",
        lambda d: d["files"][0].update(sha256="NOT-A-SHA"))
    with pytest.raises(PackInvalid, match="sha256"):
        read_pack(tmp_path / "pack")


def test_duplicate_file_path_refuses(tmp_path):
    _write_raw_pack(tmp_path / "pack", {"skills/x/SKILL.md": b"x"})
    _rewrite_manifest(
        tmp_path / "pack",
        lambda d: d["files"].append(dict(d["files"][0])))
    with pytest.raises(PackInvalid, match="重复"):
        read_pack(tmp_path / "pack")


def test_kinds_must_match_files(tmp_path):
    """``kinds`` 是推导物:声明与清单不一致 = 清单自相矛盾。"""
    _write_raw_pack(tmp_path / "pack", {"skills/x/SKILL.md": b"x"})
    _rewrite_manifest(tmp_path / "pack", lambda d: d.update(kinds=["decisions"]))
    with pytest.raises(PackInvalid, match="kinds"):
        read_pack(tmp_path / "pack")


def test_exported_at_and_tool_version_must_be_present(tmp_path):
    _write_raw_pack(tmp_path / "pack", {"skills/x/SKILL.md": b"x"})
    _rewrite_manifest(tmp_path / "pack", lambda d: d.update(exported_at=""))
    with pytest.raises(PackInvalid, match="exported_at"):
        read_pack(tmp_path / "pack")


# ── 路径形状判据 ─────────────────────────────────────────


@pytest.mark.parametrize("rel,expected", [
    ("skills/loop-x/SKILL.md", "skills"),
    ("skills/loop-x/SKILL.zh.md", "skills"),
    ("kb/mini/decisions.yml", "decisions"),
    ("presets/starter/preset.yml", "presets"),
    ("skills/loop-x/notes.md", None),
    ("kb/mini/examples.yml", None),
    ("presets/starter/other.yml", None),
    ("skills/loop-x/SKILL.md/extra", None),
    ("random/file.txt", None),
])
def test_kind_of_shapes(rel, expected):
    assert kind_of(rel) == expected


@pytest.mark.parametrize("rel,ok", [
    ("skills/x/SKILL.md", True),
    ("a/b/c.yml", True),
    ("../x", False),
    ("/abs", False),
    ("a/..", False),
    ("a/./b", False),
    ("a\\b", False),
    ("", False),
])
def test_is_valid_relpath(rel, ok):
    assert is_valid_relpath(rel) is ok


# ── git_rev:如实,不编 ───────────────────────────────────


def test_git_rev_absent_is_empty(tmp_path):
    assert pack_mod.git_rev_for(tmp_path) == ""


@pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")
def test_git_rev_reads_short_head(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def _git(*args):
        return subprocess.run(["git", "-C", str(repo), *args],
                              capture_output=True, text=True, check=True)

    _git("init", "-q")
    _git("-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "--allow-empty", "-m", "init")
    rev = pack_mod.git_rev_for(repo / "sub" / "dir")
    assert rev and all(c in "0123456789abcdef" for c in rev)
    assert len(rev) >= 7


# ── 报告形状 ─────────────────────────────────────────────


def test_pack_report_counts_and_render():
    report = PackReport(action="import", pack="demo-pack", dest="/tmp/pack")
    report.add("skills", "loop-x", "imported", "落 pending")
    report.add("skills", "loop-y", "conflict", "同名已存在")
    report.add("decisions", "mini/rule-a", "invalid", "解析失败")
    report.add("presets", "starter", "skipped", "幂等重放")
    assert report.counts["imported"] == 1
    assert report.refused == 2          # conflict + invalid
    text = report.render()
    assert "demo-pack" in text and "逐条确认后才生效" in text
    assert "冲突拒载" in text and "无效拒载" in text


def test_pack_report_rejects_unknown_status():
    report = PackReport(action="import", pack="p")
    with pytest.raises(ValueError, match="unknown pack status"):
        report.add("skills", "x", "made-up")


def test_pack_statuses_closed_set():
    assert set(PACK_STATUSES) == {
        "exported", "imported", "overwritten", "skipped", "conflict", "invalid"}


def test_loaded_pack_to_dict(tmp_path):
    write_pack(tmp_path / "pack", name="demo-pack",
               files={"skills/x/SKILL.md": b"x"})
    loaded: LoadedPack = read_pack(tmp_path / "pack")
    data = loaded.to_dict()
    assert data["files"] == ["skills/x/SKILL.md"]
    assert data["extra"] == []
    assert data["manifest"]["name"] == "demo-pack"
