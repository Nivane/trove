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

from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, Protocol, runtime_checkable

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
    """One store-facing database connection (open-per-operation model).

    Mirrors the aiosqlite connection surface the stores already use so the
    store code keeps its shape: ``await backend.execute(...)``,
    ``await backend.commit()``, ``await backend.close()``.
    """

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
    async def commit(self) -> None: ...

    @abstractmethod
    async def close(self) -> None:
        """无操作语义:保留缓存连接(内存库依赖单连接存续)。"""

    @abstractmethod
    async def dispose(self) -> None:
        """真正的资源释放(进程退出/显式清理时调用)。"""


__all__ = ["StorageBackend", "StorageCursor"]
