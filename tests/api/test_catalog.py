"""Database catalog endpoint tests (read-only)."""

from __future__ import annotations


class TestCatalog:
    async def test_list_datasources(self, client):
        resp = await client.get("/v1/catalog/datasources")
        assert resp.status_code == 200
        ds = resp.json()["datasources"]
        # kb_initialized/status 是 T5 catalog 扩展的契约字段
        # (本测试的 KB 未 seed test_db → 未 init);id 是 ds_id 身份字段
        assert len(ds) == 1
        entry = ds[0]
        assert entry["name"] == "test_db"
        assert entry["default"] is True
        assert entry["type"] == "sqlite"
        assert entry["connection"] == {"path": ":memory:"}
        assert entry["kb_initialized"] is False
        assert entry["status"] == "connected"
        assert isinstance(entry["id"], str) and entry["id"]

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

    async def test_datasources_redact_credentials(self, sqlite_registry):
        from trove.core.types import DatasourceConfig

        await sqlite_registry.register(DatasourceConfig(
            name="mysql_like",
            type="sqlite",
            connection_params={
                "host": "db.internal",
                "port": 3306,
                "database": "orders",
                "user": "app",
                "password": "hunter2",
            },
        ))
        info = sqlite_registry.list_info()
        entry = next(d for d in info if d["name"] == "mysql_like")
        assert entry["connection"] == {
            "host": "db.internal",
            "port": 3306,
            "database": "orders",
            "user": "app",
        }
        assert "password" not in entry["connection"]


class TestUpload:
    async def test_upload_csv_registers_datasource(self, client):
        csv_bytes = b"name,age,city\nAlice,30,NYC\nBob,25,SF\n"
        resp = await client.post(
            "/v1/catalog/upload",
            files={"file": ("people.csv", csv_bytes, "text/csv")},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["datasource"] == "people"
        assert body["rows"] == 2
        assert body["columns"] == ["name", "age", "city"]

        ds = await client.get("/v1/catalog/datasources")
        names = [d["name"] for d in ds.json()["datasources"]]
        assert "people" in names

        tables = await client.get(
            "/v1/catalog/tables", params={"datasource": "people"}
        )
        assert any(t["name"] == "data" for t in tables.json()["tables"])

    async def test_upload_non_admin_403(self, user_client):
        resp = await user_client.post(
            "/v1/catalog/upload",
            files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")},
        )
        assert resp.status_code == 403

    async def test_upload_bad_csv_400(self, client):
        resp = await client.post(
            "/v1/catalog/upload", files={"file": ("e.csv", b"onlyheader\n", "text/csv")}
        )
        assert resp.status_code == 400

    async def test_upload_empty_400(self, client):
        resp = await client.post(
            "/v1/catalog/upload", files={"file": ("e.csv", b"", "text/csv")}
        )
        assert resp.status_code == 400
