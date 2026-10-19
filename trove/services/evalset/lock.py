"""按数据源的验收集写锁 —— 跨进程、非阻塞。

与 ``kb.locks.KbInitLock`` 同形(scoped ``fcntl.flock(LOCK_EX | LOCK_NB)``),
锁目录是**根目录下**的 ``.locks/``:``EvalsetLock(<root>)`` 的锁文件为
``<root>/.locks/<datasource>.lock``。生成/确认/编辑共用这一把锁;runner
只读启动时的快照,刻意不拿它(一场长跑不该阻塞管理员确认)。

非阻塞是契约:拿不到立刻抛 :class:`EvalsetBusy`,绝不排队等待 —— 管理端
的一个请求不该因为另一个进程正在写而挂住。锁文件留在磁盘上(持有中 unlink
有竞态,且文件本身是空的)。
"""

from __future__ import annotations

import contextlib
import fcntl
import os
from pathlib import Path

from trove.services.evalset.store import _safe_datasource


class EvalsetBusy(Exception):
    """另一个进程/实例正持有该数据源的验收集写锁。"""


class EvalsetLock:
    """每个数据源一把非阻塞 advisory lock(``<root>/.locks/<ds>.lock``)。"""

    def __init__(self, root: Path):
        self.lock_root = Path(root) / ".locks"

    def _path(self, datasource: str) -> Path:
        # 数据源名会拼进文件名 —— 与 store 用同一把尺子(路径穿越在这里
        # 同样写得出目录外的锁文件)。
        return self.lock_root / f"{_safe_datasource(datasource)}.lock"

    @contextlib.contextmanager
    def acquire(self, datasource: str):
        path = self._path(datasource)
        self.lock_root.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise EvalsetBusy(
                    f"evalset is being modified for datasource {datasource!r}"
                ) from exc
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


__all__ = ["EvalsetBusy", "EvalsetLock"]
