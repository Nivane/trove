"""Cross-process KB init lock (fcntl flock keyed by ds_id)."""

from __future__ import annotations

import pytest

from trove.services.kb.locks import KbInitBusy, KbInitLock


def test_lock_mutual_exclusion_in_process(tmp_path):
    """同一进程两把独立 fd 的同 ds_id 锁互斥（等价于两实例竞争）。"""
    lock = KbInitLock(tmp_path / ".locks")
    with lock.acquire("abc123"):
        with pytest.raises(KbInitBusy):
            with lock.acquire("abc123"):
                pass
    # 释放后可重入
    with lock.acquire("abc123"):
        pass


def test_lock_is_keyed_per_ds_id(tmp_path):
    """不同 ds_id 互不阻塞。"""
    lock = KbInitLock(tmp_path / ".locks")
    with lock.acquire("aaa"):
        with lock.acquire("bbb"):
            pass


def test_lock_files_persist_under_locks_dir(tmp_path):
    """锁文件留在 <root>/<ds_id>.lock（unlink-while-held 有竞态,刻意不删）。"""
    root = tmp_path / ".locks"
    lock = KbInitLock(root)
    with lock.acquire("abc123"):
        pass
    assert (root / "abc123.lock").exists()