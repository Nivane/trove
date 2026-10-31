"""``migrate_semantics_v02.py`` / ``ossie_migrate.convert_text`` 测试。

读端对 v0.1 包装是硬拒绝,所以迁移器是**唯一**的根形状升级通道 —— 正确性
由四组断言钉住:

1. **转得对**:去包装、去缩进、``version`` 升到最顶,注释/键序保留;
2. **不猜**:空包装 / 多模型 / 非映射 / 坏版本一律拒绝,绝不写出半份模型;
3. **往返等价**:转换后 YAML 数据与旧模型逐键相等,且 ``parse_ossie``
   读出的条目集合一致(两道回环在纯函数内部);
4. **CLI 姿态**:默认 dry-run、``--check`` 有漂移退出码 1、``--write``
   落盘后第二次零漂移(幂等)。

全部零网络零 LLM,用 tmp 目录里的 mini KB。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

from scripts.migrate_semantics_v02 import main
from trove.services.kb.ossie_migrate import MigrationError, convert_text
from trove.services.semantic_layer.ossie import OSSIE_VERSION, parse_ossie

_TARGET = f'version: "{OSSIE_VERSION}"\n'

#: 带根注释、条目内 version、根 version 在末尾的旧包装(最坏形状)。
LEGACY = """\
# 财务域语义模型(人工维护 —— 注释必须活下来)
semantic_model:
  - name: demo
    description: demo model
    datasets:
      - name: loan
        source: loan
        primary_key: [loan_id]
        fields:
          - name: loan_id
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: loan_id
      - name: account
        source: account
    metrics:
      - name: total_amount
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: SUM(loan.amount)
    topics:
      - name: lending
        datasets: [loan]
version: 0.1.2
"""


def test_legacy_wrapper_is_flattened_and_comments_survive():
    new_text, changed = convert_text(LEGACY)

    assert changed
    assert new_text.startswith(_TARGET)
    assert "# 财务域语义模型(人工维护 —— 注释必须活下来)" in new_text
    doc = yaml.safe_load(new_text)
    assert "semantic_model" not in doc
    assert doc["name"] == "demo"
    assert [d["name"] for d in doc["datasets"]] == ["loan", "account"]
    assert [m["name"] for m in doc["metrics"]] == ["total_amount"]
    assert [t["name"] for t in doc["topics"]] == ["lending"]
    # 根 version 已挪到文件最顶,原位置不再有第二个 version 行
    assert new_text.count("version:") == 1


def test_converted_text_parses_back_to_the_same_model():
    new_text, _ = convert_text(LEGACY)

    model = parse_ossie(new_text)

    assert model.name == "demo"
    assert {d.name for d in model.datasets} == {"loan", "account"}
    assert {m.name for m in model.metrics} == {"total_amount"}
    assert {t.name for t in model.topics} == {"lending"}
    assert model.version == OSSIE_VERSION


def test_entry_level_version_is_hoisted():
    legacy = (
        "semantic_model:\n"
        "  - name: demo\n"
        "    version: 0.1.0\n"
        "    datasets: []\n"
    )
    new_text, changed = convert_text(legacy)

    assert changed
    assert new_text.startswith(_TARGET)
    assert new_text.count("version:") == 1
    assert yaml.safe_load(new_text) == {
        "version": OSSIE_VERSION, "name": "demo", "datasets": []}


@pytest.mark.parametrize("legacy", [
    "semantic_model: []\n",
    "semantic_model:\n",
    "semantic_model: {}\n",
])
def test_empty_wrapper_is_refused(legacy):
    with pytest.raises(MigrationError):
        convert_text(legacy)


def test_multi_model_is_refused():
    legacy = (
        "semantic_model:\n"
        "  - name: one\n"
        "  - name: two\n"
    )
    with pytest.raises(MigrationError, match="单模型"):
        convert_text(legacy)


def test_unknown_root_keys_are_refused_not_guessed():
    legacy = (
        "theme: dark\n"
        "semantic_model:\n"
        "  - name: demo\n"
    )
    with pytest.raises(MigrationError, match="未知键"):
        convert_text(legacy)


def test_newer_version_is_refused():
    text = 'version: "99.0"\nname: demo\ndatasets: []\n'
    with pytest.raises(MigrationError):
        convert_text(text)


def test_flat_document_at_current_version_is_a_noop():
    text = f'{_TARGET}name: demo\ndatasets: []\n'
    new_text, changed = convert_text(text)

    assert changed is False
    assert new_text == text


def test_older_flat_document_is_adopted_unchanged():
    """更老的扁平版是合法输入(adopt),迁移器不改写它。"""
    text = 'version: "0.1.0"\nname: demo\ndatasets: []\n'
    new_text, changed = convert_text(text)

    assert changed is False
    assert new_text == text


def test_flat_document_without_version_gets_one_line_on_top():
    text = "name: demo\ndatasets: []\n"
    new_text, changed = convert_text(text)

    assert changed
    assert new_text == _TARGET + text
    assert parse_ossie(new_text).name == "demo"


# ── CLI ──────────────────────────────────────────────────


def _run(monkeypatch, *argv: str) -> int:
    monkeypatch.setattr(sys, "argv", ["migrate_semantics_v02.py", *argv])
    return main()


def test_cli_dry_run_does_not_write_and_check_exits_1(tmp_path, monkeypatch, capsys):
    ds = tmp_path / "demo"
    ds.mkdir()
    target = ds / "semantics.yml"
    target.write_text(LEGACY, encoding="utf-8")

    assert _run(monkeypatch, "--kb-dir", str(tmp_path), "--all", "--check") == 1

    assert target.read_text(encoding="utf-8") == LEGACY
    out = capsys.readouterr().out
    assert f"migrate {target}" in out
    assert "待迁移 1 个文件" in out


def test_cli_write_migrates_and_second_run_is_clean(tmp_path, monkeypatch, capsys):
    ds = tmp_path / "demo"
    ds.mkdir()
    target = ds / "semantics.yml"
    target.write_text(LEGACY, encoding="utf-8")

    assert _run(monkeypatch, "--kb-dir", str(tmp_path), "--all", "--write") == 0

    migrated = target.read_text(encoding="utf-8")
    assert migrated.startswith(_TARGET)
    assert "semantic_model" not in migrated
    assert "已迁移 1 个文件" in capsys.readouterr().out

    # 幂等:第二次 --check 无漂移
    assert _run(monkeypatch, "--kb-dir", str(tmp_path), "--all", "--check") == 0
    assert target.read_text(encoding="utf-8") == migrated


def test_cli_refusal_exits_1(tmp_path, monkeypatch, capsys):
    ds = tmp_path / "demo"
    ds.mkdir()
    (ds / "semantics.yml").write_text("semantic_model: []\n", encoding="utf-8")

    assert _run(monkeypatch, "--kb-dir", str(tmp_path), "--all", "--check") == 1

    err = capsys.readouterr().err
    assert "REFUSED" in err


def test_cli_missing_file_exits_2(tmp_path, monkeypatch):
    assert _run(
        monkeypatch, "--kb-dir", str(tmp_path), "--datasource", "ghost",
        "--check",
    ) == 2


def test_cli_preserves_file_mode(tmp_path, monkeypatch):
    ds = tmp_path / "demo"
    ds.mkdir()
    target = ds / "semantics.yml"
    target.write_text(LEGACY, encoding="utf-8")
    target.chmod(0o640)

    assert _run(monkeypatch, "--kb-dir", str(tmp_path), "--all", "--write") == 0

    assert target.stat().st_mode & 0o777 == 0o640


def test_repo_knowledge_bases_are_already_flat():
    """仓库内三份 KB 与 guide snapshot 都必须已是迁移后的形状。"""
    root = Path(__file__).resolve().parents[2]
    paths = sorted((root / ".trove" / "kb").glob("*/semantics.yml"))
    paths += sorted(
        (root / "scripts" / "guide_snapshot" / "root" / ".trove" / "kb")
        .glob("*/semantics.yml"))
    assert paths
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert convert_text(text)[1] is False, f"{path} still needs migration"
        assert yaml.safe_load(text)["version"] == OSSIE_VERSION
