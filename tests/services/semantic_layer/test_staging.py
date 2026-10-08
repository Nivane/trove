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
