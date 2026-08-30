"""SQLite storage backend — local / test fallback behind StorageBackend.

Production targets Postgres (``PostgresBackend``); this backend keeps the
repo's zero-network test constraint (``tests/conftest.py``) by running the
same store code against in-memory SQLite. Same protocol, same ``?``
placeholders, same DDL — only the driver differs.

Selection: ``StorageBackend.from_url`` picks the backend by URL scheme —
``postgresql://`` → Postgres, everything else (file path / ``:memory:`` /
``sqlite://``) → SQLite. Stores keep their ``db_path`` constructor param so
tests that pass a ``tmp_path / "x.db"`` keep working unchanged.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, AsyncIterator

import aiosqlite

from trove.storage.backends.base import StorageBackend, StorageCursor
from trove.storage.backends.postgres import PostgresBackend

# 生产路由:postgres 家族 URL → PG;其余(sqlite 文件/内存路径)→ SQLite。
_POSTGRES_SCHEMES = ("postgresql://", "postgres://", "pg://")


class _SqliteCursor(StorageCursor):
    def __init__(self, cur: Any):
        self._cur = cur

    @property
    def lastrowid(self) -> int | None:
        return self._cur.lastrowid

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    async def fetchone(self) -> tuple | None:
        return await self._cur.fetchone()

    async def fetchall(self) -> list[tuple]:
        return await self._cur.fetchall()

    def __aiter__(self) -> AsyncIterator[tuple]:
        return self._cur.__aiter__()

    async def __aenter__(self) -> "_SqliteCursor":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._cur.close()


class SqliteBackend(StorageBackend):
    """aiosqlite-backed backend (in-memory or file)."""

    def __init__(self, db_path: str = ":memory:"):
        # sqlite:// 前缀剥掉,保留相对/绝对路径或 :memory:
        if db_path.startswith("sqlite://"):
            db_path = db_path[len("sqlite://"):]
        self.db_path = db_path or ":memory:"
        self._conn: aiosqlite.Connection | None = None

    async def _connect(self) -> aiosqlite.Connection:
        if self._conn is not None:
            return self._conn
        # 文件后端自动创建父目录(原 store 的 _conn 均含 mkdir)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        return self._conn

    async def execute(
        self, sql: str, params: tuple = (), *, need_lastrowid: bool = False,
    ) -> StorageCursor:
        conn = await self._connect()
        cur = await conn.execute(sql, params)
        return _SqliteCursor(cur)

    async def executemany(self, sql: str, seq_of_params: list[tuple]) -> None:
        conn = await self._connect()
        await conn.executemany(sql, seq_of_params)
        await conn.commit()

    async def executescript(self, script: str) -> None:
        conn = await self._connect()
        await conn.executescript(script)

    async def commit(self) -> None:
        if self._conn is not None:
            await self._conn.commit()

    async def close(self) -> None:
        """无操作:保留缓存连接(内存库依赖单连接存续,store 每操作调用)。"""

    async def dispose(self) -> None:
        """真正的资源释放(进程退出/显式清理时调用)。"""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None


def build_backend(target: str | Any) -> StorageBackend:
    """统一后端工厂:按目标选择 PG / SQLite。

    Args:
        target: ``postgresql://user:pass@host/db`` → PostgresBackend;
            SQLite 文件路径 / ``:memory:`` / ``sqlite://`` → SqliteBackend。

    Returns:
        就绪的 StorageBackend(尚未打开连接,open-per-operation)。
    """
    target = str(target)
    if any(target.startswith(s) for s in _POSTGRES_SCHEMES):
        return PostgresBackend(target)
    return SqliteBackend(target)


# 统一存储目标解析:生产默认 PG(环境变量 TROVE_STORAGE_URL),未设置时
# 回落 SQLite 内存库(本地/测试零网络约束)。内部 store 用这个解析 db_path。
STORAGE_URL_ENV = "TROVE_STORAGE_URL"


def storage_url() -> str:
    """返回生产存储 URL(环境变量),空 = 未配置(调用方决定 SQLite 回落)。"""
    import os

    return os.environ.get(STORAGE_URL_ENV, "").strip()


def resolve_backend(db_path: str | Any) -> StorageBackend:
    """内部 store 的默认后端解析:配置了 PG URL → PG;否则按 db_path。

    Args:
        db_path: store 构造传入的路径/URL。当 ``TROVE_STORAGE_URL`` 设置时
            一律走 PG(企业生产);否则回落到 SqliteBackend(测试/本地)。
    """
    url = storage_url()
    if url:
        return build_backend(url)
    return build_backend(db_path)


def is_postgres(target: str) -> bool:
    return any(str(target).startswith(s) for s in _POSTGRES_SCHEMES)


__all__ = ["SqliteBackend", "build_backend", "is_postgres"]
