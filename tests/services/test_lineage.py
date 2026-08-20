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