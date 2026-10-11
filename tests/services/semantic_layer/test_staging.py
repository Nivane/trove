"""隔离区读写 —— 快照往返、缺失如实返回 None（半损不静默当空）、按天清理。"""
from __future__ import annotations

import os
import time
from pathlib import Path

from trove.services.semantic_layer.staging import StagingArea


def test_stage_and_read_roundtrip(tmp_path: Path):
    area = StagingArea(tmp_path / "kb")
    area.stage("chg-1", base_text="semantic_model: []\n", after_text="semantic_model:\n- name: d\n")
    assert area.read_base("chg-1") == "semantic_model: []\n"
    assert area.read_after("chg-1") == "semantic_model:\n- name: d\n"


def test_missing_snapshot_is_none_not_empty(tmp_path: Path):
    """半损（目录在、文件缺）= None。「读不到」与「读到了、是空文档」必须分得开。"""
    area = StagingArea(tmp_path / "kb")
    area.root("chg-1").mkdir(parents=True)
    assert area.read_base("chg-1") is None
    assert area.read_after("chg-1") is None


def test_verification_roundtrip(tmp_path: Path):
    area = StagingArea(tmp_path / "kb")
    area.write_verification("chg-1", {"verdict": "neutral", "replayed": []})
    assert area.read_verification("chg-1") == {"verdict": "neutral", "replayed": []}
    assert area.read_verification("chg-2") is None


def test_prune_removes_old_keeps_fresh(tmp_path: Path):
    area = StagingArea(tmp_path / "kb")
    area.stage("old", base_text="a", after_text="b")
    area.stage("fresh", base_text="a", after_text="b")
    stale = time.time() - 40 * 86400
    os.utime(area.root("old"), (stale, stale))
    assert area.prune(30) == 1
    assert not area.root("old").exists()
    assert area.root("fresh").exists()


def test_prune_zero_days_keeps_everything(tmp_path: Path):
    """retain_days <= 0 = 不清理（与 assets/events_retention_days 的语义一致）。"""
    area = StagingArea(tmp_path / "kb")
    area.stage("old", base_text="a", after_text="b")
    stale = time.time() - 400 * 86400
    os.utime(area.root("old"), (stale, stale))
    assert area.prune(0) == 0
    assert area.root("old").exists()


def test_staging_never_enters_gitignore_scope():
    """I3 的仓库侧承诺：根 .gitignore 把 .staging/ 排除（本测试钉住条目在不在）。"""
    root = Path(__file__).resolve().parents[3]
    text = (root / ".gitignore").read_text(encoding="utf-8")
    assert ".trove/kb/*/.staging/" in text


# ── N12:新声明保存 → 快照 → 回滚,字节还原 ──────────────────

_SAVED_YAML = """version: 0.2.0.dev0
semantic_model:
  - name: fin
    datasets:
      - name: loan
        source: loan
        primary_key: [loan_id]
        entity:
          display_name: 贷款
          natural_keys: [loan_id]
          aliases: [借款]
        fields:
          - name: loan_id
            expression: {dialects: [{dialect: ANSI_SQL, expression: loan_id}]}
    relationships:
      - name: loan_to_loan
        from: loan
        to: loan
        from_columns: [loan_id]
        to_columns: [loan_id]
        display_name: 属于
    hierarchies:
      - name: product_hierarchy
        display_name: 产品层级
        dataset: loan
        levels: [loan_id]
    metrics: []
"""


def test_new_declarations_survive_snapshot_and_rollback(tmp_path: Path):
    """N12:含三块声明(or 任何新块)的 semantics.yml 保存后,

    快照是**字节级**的(base/after 原样落盘)—— 快照与回滚都不理解 YAML、
    不重排键,所以新增块逐字节存活;回滚 = 按 base 快照还原,结果与保存前
    逐字节一致(回滚是字节还原,不是「语义等价」的近似)。
    """
    target = tmp_path / "kb" / "fin" / "semantics.yml"
    target.parent.mkdir(parents=True)
    target.write_text(_SAVED_YAML, encoding="utf-8")
    before = target.read_bytes()

    area = StagingArea(tmp_path / "kb")
    after_text = _SAVED_YAML.replace("display_name: 贷款", "display_name: 信贷")
    assert after_text != _SAVED_YAML  # 替换确实生效(fixture 漂移会红)
    area.stage("chg-n12", base_text=before.decode("utf-8"), after_text=after_text)

    # 快照保真:新块原样在快照里(解析-再序列化会把注释/键序/流式写法吃掉)
    assert area.read_base("chg-n12").encode("utf-8") == before
    snapshot_after = area.read_after("chg-n12")
    assert "entity:" in snapshot_after
    assert "display_name: 属于" in snapshot_after
    assert "hierarchies:" in snapshot_after

    # 回滚:按 base 快照字节还原 → 与保存前逐字节一致
    target.write_text(area.read_base("chg-n12"), encoding="utf-8")
    assert target.read_bytes() == before


def test_new_declarations_snapshot_is_not_parse_normalized(tmp_path: Path):
    """快照 = 作者写下的字节(注释/键序/流式都保留),不是解析产物。"""
    text = "# 作者注:层级声明见下\n" + _SAVED_YAML
    area = StagingArea(tmp_path / "kb")
    area.stage("chg-raw", base_text="", after_text=text)
    assert area.read_after("chg-raw") == text
    assert area.read_after("chg-raw").startswith("# 作者注")
