"""Retrieval query hit log — the feedback-loop ground truth for eval/tuning.

Every ``HybridStore.recall`` records one row (best-effort, never raises) with
the per-query meta: channel branch sizes (keyword / dense / learned-sparse),
the RRF-fused order, the reranked order, whether the reranker actually ran, and
latency. This is the "query_log → 评测集 → 权重/参数调优" closed loop's data
source (consumed by ``scripts/eval_hybrid_retrieval.py`` and
``scripts/tune_rrf.py``).
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import aiosqlite

from trove.core.logging import get_logger

logger = get_logger(__name__)

_CREATE = """
CREATE TABLE IF NOT EXISTS retrieval_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    datasource TEXT NOT NULL,
    query TEXT NOT NULL,
    branch_sizes TEXT NOT NULL DEFAULT '[]',
    rrf_top TEXT NOT NULL DEFAULT '[]',
    rerank_top TEXT NOT NULL DEFAULT '[]',
    rerank_used INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_retrieval_log_ds ON retrieval_log(datasource);
"""


class QueryLogRecorder:
    """Append-only hit logger backed by a StorageBackend (SQLite local / PG)."""

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        from trove.storage.backends import resolve_backend

        self._backend = resolve_backend(str(path))
        self._ready = False

    @classmethod
    def for_home(cls, home: str | Path) -> "QueryLogRecorder":
        return cls(Path(home) / "retrieval" / "query_log.sqlite")

    async def _ensure(self) -> None:
        if self._ready:
            return
        await self._backend.executescript(_CREATE)
        self._ready = True

    async def record(
        self, query: str, datasource: str, meta: dict,
    ) -> None:
        """Append one retrieval hit row. meta = recall() 的 meta dict。"""
        try:
            await self._ensure()
            await self._backend.execute(
                "INSERT INTO retrieval_log "
                "(ts, datasource, query, branch_sizes, rrf_top, rerank_top, "
                " rerank_used, latency_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    str(datasource or ""),
                    str(query or "")[:1000],
                    json.dumps(meta.get("branch_sizes", [])),
                    json.dumps(meta.get("rrf_ids", [])),
                    json.dumps(meta.get("rerank_ids", [])),
                    1 if meta.get("rerank_used") else 0,
                    int(meta.get("latency_ms", 0)),
                ),
            )
            await self._backend.commit()
        except Exception as e:  # 检索日志失败绝不影响检索本身
            logger.warning("query log write failed: %s", e)
