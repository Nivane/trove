"""Lineage service + parse-layer tests (deterministic, zero LLM, tmp SQLite)."""

from __future__ import annotations

import pytest

from trove.services.lineage.parse import analyze_query, normalization_key
from trove.services.lineage.service import LineageService


# ── parse layer ─────────────────────────────────────────


class TestAnalyzeQuery:
    def test_join_projection_mapping(self):
        d = analyze_query(
            "SELECT c.client_name, count(l.loan_id) AS cnt "
            "FROM client c JOIN loan l ON c.client_id = l.client_id "
            "GROUP BY c.client_name"
        )
        assert d.tables_read == ["client", "loan"]
        assert ("client", "client_name") in d.columns_read
        assert ("loan", "loan_id") in d.columns_read
        outs = dict((o, [s.column for s in src]) for o, src in d.outputs)
        assert outs["client_name"] == ["client_name"]  # alias→base resolution
        assert outs["cnt"] == ["loan_id"]

    def test_create_view_kind_and_name(self):
        d = analyze_query(
            "CREATE VIEW loan_stats AS SELECT client_name, SUM(amount) AS total "
            "FROM loan GROUP BY client_name"
        )
        assert d.kind == "create_view"
        assert d.name == "loan_stats"
        assert d.tables_read == ["loan"]

    def test_create_table_as_kind(self):
        d = analyze_query("CREATE TABLE t2 AS SELECT a FROM t")
        assert d.kind == "create_table_as"
        assert d.name == "t2"

    def test_unqualified_column_resolves_to_single_base(self):
        d = analyze_query("SELECT amount FROM loan WHERE status = 1")
        assert ("loan", "amount") in d.columns_read
        outs = dict((o, [(s.table, s.column) for s in src]) for o, src in d.outputs)
        assert outs["amount"] == [("loan", "amount")]

    def test_unparseable_returns_none(self):
        assert analyze_query("this is not sql") is None
        assert analyze_query("") is None

    def test_normalization_key_strips_formatting_and_case(self):
        assert normalization_key("SELECT  a, b FROM t -- note") == normalization_key(
            "select A, B FROM T"
        )
        assert normalization_key("") == ""


# ── service layer ───────────────────────────────────────


class TestLineageService:
    async def test_table_lineage_roundtrip(self, tmp_path):
        svc = LineageService(tmp_path)
        await svc.ingest_definition(
            "CREATE VIEW loan_stats AS SELECT client_name, SUM(amount) AS total "
            "FROM loan GROUP BY client_name",
            "financial", dialect="sqlite",
        )
        upstream = await svc.table_upstream("financial", "loan_stats")
        assert [u["name"] for u in upstream] == ["loan_stats"]
        downstream = await svc.table_downstream("financial", "loan")
        assert any(c["name"] == "loan_stats" for c in downstream)

    async def test_column_lineage_producers_and_consumers(self, tmp_path):
        svc = LineageService(tmp_path)
        await svc.ingest_definition(
            "CREATE VIEW loan_stats AS SELECT client_name, SUM(amount) AS total "
            "FROM loan GROUP BY client_name",
            "financial", dialect="sqlite",
        )
        await svc.record_query(
            "SELECT client_name, total FROM loan_stats", "financial", dialect="sqlite",
        )
        cell = await svc.column_lineage("financial", "loan_stats", "total")
        sources = [s for p in cell["producers"] for s in p["sources"]]
        assert any(s["table"] == "loan" and s["column"] == "amount" for s in sources)
        assert len(cell["consumers"]) == 1
        assert cell["consumers"][0]["runs"] == 1

    async def test_query_deduped_by_shard(self, tmp_path):
        svc = LineageService(tmp_path)
        sql = "SELECT amount FROM loan WHERE status = 1"
        for _ in range(3):
            await svc.record_query(sql, "financial", dialect="sqlite")
        consumers = await svc.table_downstream("financial", "loan")
        runs = [c["runs"] for c in consumers if c["kind"] == "query"]
        assert runs == [3]

    async def test_definitions_yaml_synced_by_mtime(self, tmp_path):
        svc = LineageService(tmp_path)
        path = svc.definitions_yaml("financial")
        path.parent.mkdir(parents=True)
        path.write_text(
            "definitions:\n"
            "  - sql: |\n"
            "      CREATE VIEW loan_stats AS SELECT client_name, SUM(amount) AS total FROM loan GROUP BY client_name\n"
        )
        await svc.ensure_synced("financial")
        upstream = await svc.table_upstream("financial", "loan_stats")
        assert upstream, "YAML definitions should be ingested"
        # unchanged mtime → no-op reload (still readable)
        await svc.ensure_synced("financial")

    async def test_clear_scoped(self, tmp_path):
        svc = LineageService(tmp_path)
        await svc.ingest_definition(
            "CREATE VIEW loan_stats AS SELECT amount FROM loan", "financial",
        )
        await svc.record_query("SELECT amount FROM loan", "other")
        await svc.clear("financial")
        assert not await svc.table_upstream("financial", "loan_stats")
        consumers = await svc.table_downstream("other", "loan")
        assert consumers

    async def test_no_facts_returns_empty(self, tmp_path):
        svc = LineageService(tmp_path)
        cell = await svc.column_lineage("financial", "loan", "amount")
        assert cell == {"producers": [], "consumers": []}
        assert await svc.table_upstream("financial", "loan") == []
        assert await svc.table_downstream("financial", "loan") == []


async def _sync_row(svc: LineageService) -> dict:
    """lineage_sync 里那条同步记录(直接读库,不走服务接口)。"""
    import aiosqlite

    async with aiosqlite.connect(str(svc.db_path)) as db:
        db.row_factory = aiosqlite.Row
        async with await db.execute(
            "SELECT file_path, mtime, size, digest FROM lineage_sync",
        ) as cursor:
            row = await cursor.fetchone()
    return dict(row)


async def _definition_row(svc: LineageService, name: str) -> dict:
    import aiosqlite

    async with aiosqlite.connect(str(svc.db_path)) as db:
        db.row_factory = aiosqlite.Row
        async with await db.execute(
            "SELECT sql, kind, created_at, updated_at FROM lineage_definitions "
            "WHERE datasource = ? AND name = ?", ("financial", name),
        ) as cursor:
            row = await cursor.fetchone()
    assert row is not None, f"{name} 没进镜像"
    return dict(row)


class TestDefinitionsSyncDigest:
    """定义文件同步的判据是内容摘要,不是 mtime(C3)。"""

    async def test_same_mtime_different_content_resyncs(self, tmp_path):
        """mtime 相同、内容不同 → 定义必须重新同步(改前必红)。"""
        import os

        svc = LineageService(tmp_path)
        path = svc.definitions_yaml("financial")
        path.parent.mkdir(parents=True)
        path.write_text(
            "definitions:\n"
            "  - sql: |\n"
            "      CREATE VIEW first_view AS SELECT amount FROM loan\n"
        )
        await svc.ensure_synced("financial")
        assert await svc.table_upstream("financial", "first_view")

        # 内容换成另一个视图名,mtime 按原样写回(cp -p / rsync -t 的效果)
        stat = path.stat()
        path.write_text(
            "definitions:\n"
            "  - sql: |\n"
            "      CREATE VIEW second_view AS SELECT amount FROM loan\n"
        )
        os.utime(path, (stat.st_atime, stat.st_mtime))
        await svc.ensure_synced("financial")

        assert await svc.table_upstream("financial", "second_view"), (
            "mtime 相等不该让同步跳过 —— 判据是内容摘要"
        )
        assert not await svc.table_upstream("financial", "first_view"), (
            "重建式同步:旧定义必须消失,不能两份都留着"
        )

    async def test_unchanged_content_is_a_noop(self, tmp_path):
        """内容没变 → 不重建(判据换成摘要,不能变成"每次都重灌")。

        重建是 DELETE 全量 + 重新 INSERT,`updated_at` 会跟着变 —— 拿它当
        观测点,不用打桩就能看出重建有没有发生。
        """
        svc = LineageService(tmp_path)
        path = svc.definitions_yaml("financial")
        path.parent.mkdir(parents=True)
        path.write_text(
            "definitions:\n"
            "  - sql: |\n"
            "      CREATE VIEW stable_view AS SELECT amount FROM loan\n"
        )
        await svc.ensure_synced("financial")
        first = await _definition_row(svc, "stable_view")

        await svc.ensure_synced("financial")
        assert await _definition_row(svc, "stable_view") == first, (
            "内容没变就不该重建 —— updated_at 动了说明重灌了一遍"
        )

    async def test_digest_column_is_backfilled_for_legacy_db(self, tmp_path):
        """旧库(2 列 lineage_sync)升级:就地补列 + 按 mtime 可信度回填。

        回填的用处不是"表里多个字段",是**升级本身不重建**:补上摘要后判据
        立刻命中,已入库的定义原样留着(updated_at 不动)。不回填就会白白
        删一遍再灌一遍。
        """
        import aiosqlite

        svc = LineageService(tmp_path)
        path = svc.definitions_yaml("financial")
        path.parent.mkdir(parents=True)
        path.write_text(
            "definitions:\n"
            "  - sql: |\n"
            "      CREATE VIEW legacy_view AS SELECT amount FROM loan\n"
        )
        # 升级前的镜像:定义已入库、同步表只有两列,文件此后没动过
        svc.lineage_dir.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(str(svc.db_path)) as db:
            await db.execute(
                "CREATE TABLE lineage_definitions (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "datasource TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL, "
                "sql TEXT NOT NULL, dialect TEXT NOT NULL DEFAULT 'sqlite', "
                "digest_json TEXT NOT NULL, created_at TEXT NOT NULL, "
                "updated_at TEXT NOT NULL, UNIQUE(datasource, name))")
            await db.execute(
                "CREATE TABLE lineage_query_log (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "datasource TEXT NOT NULL, shard TEXT NOT NULL, sql TEXT NOT NULL, "
                "dialect TEXT NOT NULL DEFAULT 'sqlite', digest_json TEXT NOT NULL, "
                "first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, "
                "runs INTEGER NOT NULL DEFAULT 1, UNIQUE(datasource, shard))")
            await db.execute(
                "CREATE TABLE lineage_sync (file_path TEXT PRIMARY KEY, mtime REAL NOT NULL)")
            await db.execute(
                "INSERT INTO lineage_definitions (datasource, name, kind, sql, dialect, "
                "digest_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("financial", "legacy_view", "create_view",
                 "CREATE VIEW legacy_view AS SELECT amount FROM loan", "sqlite",
                 "null", "2020-01-01T00:00:00+00:00", "2020-01-01T00:00:00+00:00"),
            )
            await db.execute(
                "INSERT INTO lineage_sync (file_path, mtime) VALUES (?, ?)",
                (str(path), path.stat().st_mtime),
            )
            await db.commit()

        await svc.ensure_synced("financial")
        row = await _sync_row(svc)
        assert row["digest"], "mtime 对得上 → 摘要可信,应就地补上"
        assert row["size"] == path.stat().st_size
        assert (await _definition_row(svc, "legacy_view"))["updated_at"] == (
            "2020-01-01T00:00:00+00:00"
        ), "摘要已补上 → 判据命中 → 不该重建这份没变过的定义"
