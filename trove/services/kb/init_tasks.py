"""In-process KB init task registry — 异步 /kb init + 进度轮询。

`POST /admin/datasources/{name}/kb/init` 现在立即返回 202 + task_id,真正
的初始化在后台 ``asyncio.create_task`` 跑;前端轮询
``GET .../kb/init/status`` 拿进度。任务状态是进程内存的(单 serve 实例
约定,与其它进程内状态一致);跨实例初始化仍由 ``KbInitLock``(flock)
串行,本注册表只反映发起方实例。

写盘原子段保证:进程被杀/中断只会落在"全无/全有",不会半成品。
"""
from __future__ import annotations

import time
import uuid
from collections import deque
from threading import Lock
from typing import Any

# 任务保留策略:完成后保留一段时间供前端收尾轮询/审计,超时清理
_TASK_TTL_S = 3600
_MAX_TASKS = 100


class InitTaskStore:
    """按 datasource 键控的最近任务注册表(进程内,线程安全)。"""

    def __init__(self, ttl_s: int = _TASK_TTL_S, max_entries: int = _MAX_TASKS) -> None:
        self._tasks: dict[str, dict[str, Any]] = {}
        self._by_ds: dict[str, str] = {}  # datasource → 最近 task_id
        self._order: deque[str] = deque()
        self._background: dict[str, Any] = {}  # task_id → asyncio.Task(防 GC)
        self._ttl_s = ttl_s
        self._max = max_entries
        self._lock = Lock()

    # ── 写 ────────────────────────────────────────────────

    def create(self, datasource: str, ds_id: str = "") -> dict[str, Any]:
        """登记新运行任务;同源已有 running 任务 → 返回 None(调用方 409)。"""
        with self._lock:
            self._prune_locked()
            existing = self._by_ds.get(datasource)
            if existing and self._tasks.get(existing, {}).get("status") == "running":
                return None
            task_id = uuid.uuid4().hex[:12]
            now = time.time()
            task: dict[str, Any] = {
                "id": task_id,
                "datasource": datasource,
                "ds_id": ds_id,
                "status": "running",
                "stage": "queued",
                "progress": 0,
                "detail": "",
                "summary": "",
                "error": "",
                "started_at": now,
                "finished_at": None,
            }
            self._tasks[task_id] = task
            self._by_ds[datasource] = task_id
            self._order.append(task_id)
            return task

    def update(self, task_id: str, **fields: Any) -> None:
        """init_kb 的 progress 回调:合并 stage/progress/detail。"""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            for k in ("stage", "progress", "detail"):
                if k in fields:
                    task[k] = fields[k]

    def done(self, task_id: str, summary: str) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task["status"] = "done"
            task["summary"] = summary
            task["progress"] = 100
            task["stage"] = "done"
            task["finished_at"] = time.time()

    def fail(self, task_id: str, error: str) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task["status"] = "error"
            task["error"] = error
            task["stage"] = "error"
            task["finished_at"] = time.time()

    def bind_background(self, task_id: str, task: Any) -> None:
        """持有 asyncio.Task 引用防止被 GC;完成即释放。"""
        with self._lock:
            self._background[task_id] = task

    def release_background(self, task_id: str) -> None:
        with self._lock:
            self._background.pop(task_id, None)

    def reset(self) -> None:
        """清空全部任务(测试隔离用)。"""
        with self._lock:
            self._tasks.clear()
            self._by_ds.clear()
            self._order.clear()
            self._background.clear()

    # ── 读 ────────────────────────────────────────────────

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return dict(task) if task is not None else None

    def by_datasource(self, datasource: str) -> dict[str, Any] | None:
        with self._lock:
            task_id = self._by_ds.get(datasource)
            task = self._tasks.get(task_id) if task_id else None
            return dict(task) if task is not None else None

    def _prune_locked(self) -> None:
        """容量 + TTL 清理(仅保留完成/失败任务;running 永不清理)。"""
        now = time.time()
        stale = [
            tid for tid, t in self._tasks.items()
            if t["status"] != "running"
            and (now - (t.get("finished_at") or t.get("started_at") or now)) > self._ttl_s
        ]
        for tid in stale:
            self._drop_locked(tid)
        while len(self._order) > self._max:
            oldest = self._order.popleft()
            if self._tasks.get(oldest, {}).get("status") != "running":
                self._drop_locked(oldest)

    def _drop_locked(self, task_id: str) -> None:
        task = self._tasks.pop(task_id, None)
        self._background.pop(task_id, None)
        if task is not None and self._by_ds.get(task["datasource"]) == task_id:
            self._by_ds.pop(task["datasource"], None)


init_tasks = InitTaskStore()
