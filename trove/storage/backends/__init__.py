"""Unified storage backends — one API, SQLite (test/local) or Postgres (production).

Trove's internal state historically lived in scattered per-store SQLite
files. This package gives every internal store one ``StorageBackend``
(``?``-placeholder SQL, ``lastrowid``, ``executescript`` — the backend hides
the dialect). Production targets PostgreSQL (``PostgresBackend``); the
in-memory ``SqliteBackend`` keeps the repo's zero-network test constraint.

Selection is by target string via :func:`build_backend` — see
``trove/storage/backends/sqlite.py``.
"""

from __future__ import annotations

from trove.storage.backends.base import StorageBackend, StorageCursor
from trove.storage.backends.postgres import PostgresBackend
from trove.storage.backends.sqlite import (
    SqliteBackend,
    build_backend,
    is_postgres,
    resolve_backend,
    storage_url,
)

__all__ = [
    "StorageBackend",
    "StorageCursor",
    "PostgresBackend",
    "SqliteBackend",
    "build_backend",
    "resolve_backend",
    "storage_url",
    "is_postgres",
]
