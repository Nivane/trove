"""Database catalog endpoint tests (read-only)."""

from __future__ import annotations


class TestCatalog:
    async def test_list_datasources(self, client):
        resp = await client.get("/v1/catalog/datasources")
        assert resp.status_code == 200
        ds = resp.json()["datasources"]
        assert ds == [{"name": "test_db", "default": True}]

    async def test_list_tables(self, client):
        resp = await client.get("/v1/catalog/tables")
        assert resp.status_code == 200
        tables = resp.json()["tables"]
        assert any(t["name"] == "students" for t in tables)

    async def test_table_detail(self, client):
        resp = await client.get("/v1/catalog/tables/students")
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "students"
        assert {c["name"] for c in body["columns"]} >= {"id", "name", "grade", "county"}

    async def test_table_detail_missing_404(self, client):
        resp = await client.get("/v1/catalog/tables/nope")
        assert resp.status_code == 404

    async def test_search_tables(self, client):
        resp = await client.get("/v1/catalog/search", params={"q": "stud"})
        assert resp.status_code == 200
        names = [t["name"] for t in resp.json()["tables"]]
        assert "students" in names

    async def test_table_ddl(self, client):
        resp = await client.get("/v1/catalog/tables/students/ddl")
        assert resp.status_code == 200
        ddl = resp.json()["ddl"]
        assert ddl.startswith("CREATE TABLE students")
        assert "grade" in ddl

    async def test_table_ddl_missing_404(self, client):
        resp = await client.get("/v1/catalog/tables/nope/ddl")
        assert resp.status_code == 404

    async def test_unknown_datasource_404(self, client):
        resp = await client.get("/v1/catalog/tables", params={"datasource": "nope"})
        assert resp.status_code == 404
