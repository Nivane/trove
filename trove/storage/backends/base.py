"""Unified storage backend — abstract over SQLite / PostgreSQL dialects.

Trove's internal state (sessions, checkpoints, user facts, memory, auth,
settings, jobs, lineage, query log, KB mirror) historically lived in
per-store SQLite files. This package defines one ``StorageBackend``
protocol so a store talks to a single API and the backend hides the
dialect differences:

- placeholders: SQLite ``?`` vs Postgres ``%s`` (normalized by the backend);
- auto-increment: SQLite ``INTEGER PRIMARY KEY AUTOINCREMENT`` vs Postgres
  ``IDENTITY``/``SERIAL`` — stores use a portable ``INTEGER PRIMARY KEY``
  and the backend DDL loader rewrites it for the dialect;
- ``lastrowid``: SQLite exposes it on the cursor; Postgres uses
  ``INSERT ... RETURNING <pk>`` (the backend rewrites the statement and
  materializes ``cursor.lastrowid``);
- ``executescript``: SQLite runs a multi-statement script; Postgres executes
  statements one at a time.

Stores keep writing SQL with ``?`` placeholders (portable subset); the
backend owns the translation. See ``postgres.py`` (production) and
``sqlite.py`` (test/local in-memory).
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# 作用域持有者异常未释放的等待上限:超过即抛错,把"存储整体卡死"变成一条
# 可诊断的异常(正常操作是毫秒级,60s 只可能是漏了 commit/close)。
_OP_LOCK_TIMEOUT_S = 60.0

# 存储 DDL 常量多为无分号结尾的单语句;合并多语句脚本时必须用分号分隔
# (aiosqlite.executescript 与 psycopg 都需要)。本函数在每条语句间补分号。
def script_statements(statements: list[str]) -> str:
    """把多条 DDL 语句拼成以分号分隔的脚本(每条去掉尾分号后补一个)。"""
    parts: list[str] = []
    for s in statements:
        s = (s or "").strip()
        if not s:
            continue
        s = s.rstrip(";").rstrip()
        parts.append(s)
    return ";\n".join(parts) + ";"


@runtime_checkable
class StorageCursor(Protocol):
    """Result cursor — a store-visible subset of aiosqlite's cursor."""

    @property
    def lastrowid(self) -> int | None: ...

    @property
    def rowcount(self) -> int: ...

    async def fetchone(self) -> tuple | None: ...

    async def fetchall(self) -> list[tuple]: ...

    def __aiter__(self) -> AsyncIterator[tuple]: ...

    async def __aenter__(self) -> "StorageCursor": ...

    async def __aexit__(self, exc_type, exc, tb) -> None: ...


class StorageBackend(ABC):
    """One store-facing database connection, **one operation at a time**.

    Mirrors the aiosqlite connection surface the stores already use so the
    store code keeps its shape: ``await backend.execute(...)``,
    ``await backend.commit()``, ``await backend.close()``.

    The connection is shared and long-lived (the in-memory fallback depends
    on it surviving, and PG reuses it instead of reconnecting per call), so
    there is exactly **one transaction per process**. Store operations are
    written as a bracket::

        conn = await self._conn()          # the backend itself
        try:
            await conn.execute(...)        # one or more statements
            await conn.commit()
        finally:
            await conn.close()

    Without an explicit scope those brackets interleave: two concurrent
    requests put statements into the *same* transaction, and whichever one
    commits first publishes the other's **in-flight** statements. Worse,
    nothing rolled back, so a half-finished operation's partial writes were
    published later by any unrelated commit.

    So the backend owns an **operation scope**: the first ``execute`` takes
    the scope for the current task, ``commit()`` finishes it, ``close()``
    finishes it (rolling back anything uncommitted), and a failing statement
    aborts it immediately. Two operations can therefore never share a
    transaction. Nested calls from the same task reuse the outer scope
    (store helpers such as ``_upsert_meta`` are called with the connection
    already open) — and every store operation must end in ``commit()`` or
    ``close()``, which is verified across all call sites.

    Cost note: an operation now holds its task's statements together instead
    of interleaving them. Since one connection can only run one statement at
    a time anyway, this changes the *granularity* of serialization (statement
    → operation), not the throughput ceiling.
    """

    # ── Operation scope ────────────────────────────────────

    def _init_op_scope(self) -> None:
        """初始化操作作用域状态(子类 ``__init__`` 调用)。"""
        self._op_lock: asyncio.Lock | None = None
        self._op_owner: asyncio.Task | None = None

    def _owns_op(self) -> bool:
        return self._op_owner is not None and self._op_owner is asyncio.current_task()

    async def _op_begin(self) -> None:
        """进入操作作用域:首个 execute 取锁;同一 task 的嵌套直接复用。

        同 task 复用是必需的:store 的辅助函数(``_upsert_meta``)拿着外层
        连接继续 execute,若各自取锁会**自死锁**。
        """
        task = asyncio.current_task()
        if self._op_owner is task:
            return
        if self._op_lock is None:
            self._op_lock = asyncio.Lock()
        try:
            await asyncio.wait_for(
                self._op_lock.acquire(), _OP_LOCK_TIMEOUT_S)
        except TimeoutError:
            logger.error(
                "storage operation scope held for >%ss by %r — every store "
                "operation must end with commit() or close()",
                _OP_LOCK_TIMEOUT_S, self._op_owner,
            )
            raise RuntimeError(
                "storage operation scope timed out: a previous operation "
                "never called commit() or close()"
            ) from None
        self._op_owner = task

    async def _op_end(self) -> None:
        """结束作用域并释放锁(幂等:非持有者 / 已结束 = 空操作)。"""
        if not self._owns_op():
            return
        self._op_owner = None
        if self._op_lock is not None:
            self._op_lock.release()

    async def _op_abort(self) -> None:
        """语句失败 → 立即回滚并结束作用域,半成品绝不外泄。

        不在这里收口的话,残留的在途语句会被之后**任意一次**无关的 commit
        带出去;而 ``query_log.record`` 这类 execute→commit 且吞掉异常的写法
        连 ``close()`` 都不会调用,作用域会永久泄漏、阻塞整个进程的存储。
        """
        if not self._owns_op():
            return
        try:
            await self.rollback_pending()
        except Exception as e:  # 回滚失败不应掩盖原始语句错误
            logger.warning("storage rollback after failed statement: %s", e)
        await self._op_end()

    @abstractmethod
    async def rollback_pending(self) -> None:
        """回滚未提交的写入(无未提交写入 → 空操作,不产生额外往返)。"""

    @abstractmethod
    async def execute(
        self, sql: str, params: tuple[Any, ...] = (),
        *, need_lastrowid: bool = False,
    ) -> StorageCursor:
        """Execute one statement (params use ``?`` placeholders).

        ``need_lastrowid=True`` declares the caller reads ``cursor.lastrowid``
        after an INSERT; Postgres backends rewrite to ``RETURNING <pk>``.
        """

    @abstractmethod
    async def executemany(
        self, sql: str, seq_of_params: list[tuple[Any, ...]],
    ) -> None: ...

    @abstractmethod
    async def executescript(self, script: str) -> None:
        """Run a multi-statement script (DDL)."""

    @abstractmethod
    async def commit(self) -> None:
        """提交并**结束操作作用域**(作用域的两种收尾之一)。"""

    @abstractmethod
    async def close(self) -> None:
        """结束操作作用域:未提交的写入先回滚,再释放。

        连接本身保留(内存库依赖单连接存续),语义是"这次操作到此为止",
        不是"断开连接"。从未进入作用域的调用是空操作 —— store 普遍在
        ``finally`` 里调用它。
        """

    @abstractmethod
    async def dispose(self) -> None:
        """真正的资源释放(进程退出/显式清理时调用)。"""


__all__ = ["StorageBackend", "StorageCursor"]
