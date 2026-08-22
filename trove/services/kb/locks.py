"""Cross-process KB initialization locks keyed by datasource ds_id.

Replaces the admin router's in-process ``_kb_init_inflight`` set: an
fds-scoped ``fcntl.flock`` acquired on ``<kb_dir>/.locks/<ds_id>.lock``
serializes init across multiple ``trove serve`` instances on a shared
KB volume. Lock files are intentionally left on disk (unlink-while-held
is race-prone and the files are empty).

Note: a single shared filesystem is the coordination boundary; for
object-store-only deployments the enterprise swap is a DB-backed row
lock (``SELECT ... FOR UPDATE``) or a lease via the metadata service.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
from pathlib import Path


class KbInitBusy(Exception):
    """Another process/instance holds the init lock for this datasource."""


class KbInitLock:
    """Non-blocking advisory lock per datasource identity (ds_id)."""

    def __init__(self, root: Path):
        self.lock_root = Path(root)

    def _path(self, ds_id: str) -> Path:
        return self.lock_root / f"{ds_id}.lock"

    @contextlib.contextmanager
    def acquire(self, ds_id: str):
        self.lock_root.mkdir(parents=True, exist_ok=True)
        path = self._path(ds_id)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as e:
                raise KbInitBusy(
                    f"KB init already running for datasource {ds_id}"
                ) from e
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)