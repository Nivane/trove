"""Tests for the retrieval query hit log recorder (local SQLite, aiosqlite)."""

import aiosqlite
import pytest

from trove.services.retrieval.query_log import QueryLogRecorder


async def test_recorder_appends_rows(tmp_path):
    rec = QueryLogRecorder(tmp_path / "query_log.sqlite")
    await rec.record("question one", "ds", {
        "branch_sizes": [3, 2, 1], "rrf_ids": ["a", "b"],
        "rerank_ids": ["b", "a"], "rerank_used": True, "latency_ms": 5,
    })
    await rec.record("question two", "ds", {
        "branch_sizes": [1, 1], "rrf_ids": ["c"],
        "rerank_ids": ["c"], "rerank_used": False, "latency_ms": 2,
    })
    async with aiosqlite.connect(tmp_path / "query_log.sqlite") as db:
        cur = await db.execute(
            "SELECT query, branch_sizes, rrf_top, rerank_top, rerank_used, latency_ms "
            "FROM retrieval_log ORDER BY id")
        rows = await cur.fetchall()
    assert rows[0][0] == "question one"
    assert rows[0][1] == "[3, 2, 1]"
    assert rows[0][2] == '["a", "b"]'
    assert rows[0][3] == '["b", "a"]'
    assert rows[0][4] == 1
    assert rows[0][5] == 5
    assert rows[1][0] == "question two"
    assert rows[1][4] == 0


async def test_recorder_never_raises_on_bad_meta(tmp_path):
    rec = QueryLogRecorder(tmp_path / "q.sqlite")
    await rec.record(None, "ds", {"branch_sizes": "not-a-list"})  # 应吞掉,不抛


async def test_recorder_for_home(tmp_path):
    rec = QueryLogRecorder.for_home(tmp_path)
    assert str(rec._path).endswith("retrieval/query_log.sqlite")
