"""GET /v1/admin/coverage —— 建模覆盖 + 被问未建模(设计稿 P5 §4.1②)。

纯集合差、零 LLM。本文件覆盖三条诚实判据:

1. **没有模型**是结构事实(``declared_tables: []`` → 全部表都是 uncovered),
   **有文件读不懂**是降级(``model: null`` + ``degraded[]``)—— 两条信息;
2. ``uncovered_tables`` / ``asked_unmodeled`` 是两个集合的差,**两边都在场**才算;
   catalog 不可达 → ``physical: null`` + 差为 null(绝不按「全都没建模」算);
3. ``refused`` 逐源列出未采纳的 KB 资产(文件 + 原因原文)。
"""

from __future__ import annotations

import yaml

from trove.api.routers import overview

SOURCE_KEYS = {
    "ds", "model", "physical", "uncovered_tables", "asked_unmodeled", "refused",
}


def _root(api_app):
    return api_app.state.kb.kb_dir.parent.parent


def _source(body: dict, ds: str = "test_db") -> dict:
    return next(s for s in body["sources"] if s["ds"] == ds)


async def _seed_queries(api_app, sql: str, *, runs: int = 1, datasource="test_db"):
    from trove.services.lineage.service import LineageService

    svc = LineageService(_root(api_app))
    for _ in range(runs):
        await svc.record_query(sql, datasource)


class TestCoverageShape:
    async def test_model_declared_vs_physical(self, client, api_app, api_kb):
        r = await client.get("/v1/admin/coverage")
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body) == {"generated_at", "window", "sources", "degraded"}
        assert body["generated_at"]
        assert body["window"] == "30d"          # 设计稿未写默认;30d 见端点注释
        assert body["degraded"] == []
        assert len(body["sources"]) == 1

        src = _source(body)
        assert set(src) == SOURCE_KEYS
        # 声明的数据集(= 物理锚定,source or name)与活库表集合对齐
        assert src["model"] == {
            "enabled": True, "datasets": 1, "metrics": 1,
            "declared_tables": ["students"],
        }
        assert src["physical"] == {"tables": 1, "source": "catalog"}
        assert src["uncovered_tables"] == []     # 差是真空,不是「不知道」
        assert src["asked_unmodeled"] == []
        assert src["refused"] == {"count": 0, "files": []}

    async def test_no_model_every_table_uncovered(self, client, api_app):
        """「没有语义模型」= 结构事实:declared_tables [],全部表未覆盖。"""
        body = (await client.get("/v1/admin/coverage")).json()
        src = _source(body)
        assert src["model"] == {
            "enabled": False, "datasets": 0, "metrics": 0, "declared_tables": [],
        }
        assert src["physical"] == {"tables": 1, "source": "catalog"}
        assert src["uncovered_tables"] == ["students"]
        assert src["asked_unmodeled"] == []
        assert body["degraded"] == []

    async def test_window_echoed_and_invalid_400(self, client, api_kb):
        r = await client.get("/v1/admin/coverage", params={"window": "7d"})
        assert r.status_code == 200
        assert r.json()["window"] == "7d"
        for bad in ("abc", "24", "0h", "200d"):
            r = await client.get("/v1/admin/coverage", params={"window": bad})
            assert r.status_code == 400, bad

    async def test_ds_filter_and_unknown_404(self, client, api_kb):
        body = (await client.get(
            "/v1/admin/coverage", params={"ds": "test_db"},
        )).json()
        assert [s["ds"] for s in body["sources"]] == ["test_db"]
        r = await client.get("/v1/admin/coverage", params={"ds": "nope"})
        assert r.status_code == 404


class TestCoverageSets:
    async def test_uncovered_tables_is_the_set_difference(self, client, api_app, api_kb):
        """活库多了一张没建模的表 → 只列出它,已声明的 students 不重复算。"""
        registry = api_app.state.connector_registry
        adapter = await registry.get("test_db")
        await adapter.execute("CREATE TABLE loans (id INTEGER, amount REAL)")

        body = (await client.get("/v1/admin/coverage")).json()
        src = _source(body)
        assert src["uncovered_tables"] == ["loans"]

    async def test_asked_unmodeled_minus_declared(self, client, api_app, api_kb):
        """被问过但没建模的表进 listed;已声明的 students 被差掉。"""
        await _seed_queries(api_app, "SELECT amount FROM loans", runs=2)
        await _seed_queries(api_app, "SELECT name FROM students", runs=1)

        body = (await client.get("/v1/admin/coverage")).json()
        src = _source(body)
        assert [r["table"] for r in src["asked_unmodeled"]] == ["loans"]
        loans = src["asked_unmodeled"][0]
        assert loans["queries"] == 2
        assert loans["last_asked_at"]

    async def test_refused_assets_surface_per_source(self, client, api_app, api_kb):
        """比代码新的 KB 资产:不进镜像,但必须在覆盖体检里点名(文件 + 原因)。"""
        from trove.services.kb.provenance import (
            FORMAT_VERSION,
            META_KEY,
            dump_asset,
        )

        ds_dir = api_app.state.kb.kb_dir / "test_db"
        doc = yaml.safe_load((ds_dir / "schema_notes.yml").read_text(encoding="utf-8"))
        future = yaml.safe_load(dump_asset(doc, "trove-from-the-future"))
        future[META_KEY]["format"] = FORMAT_VERSION + 5
        (ds_dir / "schema_notes.yml").write_text(
            yaml.safe_dump(future, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

        body = (await client.get("/v1/admin/coverage")).json()
        refused = _source(body)["refused"]
        assert refused["count"] == 1
        entry = refused["files"][0]
        assert entry["file"] == "test_db/schema_notes.yml"
        assert str(FORMAT_VERSION + 5) in entry["reason"]


class TestCoverageDegraded:
    async def test_catalog_unreachable_nulls_the_difference(
        self, client, api_app, api_kb, sqlite_registry, monkeypatch,
    ):
        """物理 schema 读不到 → physical null 且差不可得(不是「全都没建模」)。"""
        async def boom(_name):
            raise RuntimeError("catalog down")

        monkeypatch.setattr(sqlite_registry, "get", boom)
        r = await client.get("/v1/admin/coverage")
        assert r.status_code == 200, r.text
        src = _source(r.json())
        assert src["physical"] is None
        assert src["uncovered_tables"] is None
        assert src["model"]["enabled"] is True     # 模型是本地文件,不受影响

    async def test_unparseable_model_degrades_not_empty(
        self, client, api_app, api_kb,
    ):
        """有语义模型文件但读不懂 → model null + degraded(≠「没有模型」)。"""
        path = api_app.state.kb.kb_dir / "test_db" / "semantics.yml"
        path.write_text("{not: [valid", encoding="utf-8")

        r = await client.get("/v1/admin/coverage")
        assert r.status_code == 200, r.text
        body = r.json()
        src = _source(body)
        assert src["model"] is None
        assert src["uncovered_tables"] is None     # 模型侧缺席 → 差不可得
        assert src["asked_unmodeled"] is None
        assert src["physical"] is not None          # 单腿降级不传染
        entry = next(d for d in body["degraded"] if d["ds"] == "test_db")
        assert entry["error"] == "ValueError"       # 只报类型名
        assert entry["at"]

    async def test_registry_failure_sources_null(self, client, api_app, monkeypatch):
        def boom(_request):
            raise RuntimeError("registry down")

        monkeypatch.setattr(overview, "_datasource_names", boom)
        r = await client.get("/v1/admin/coverage")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["sources"] is None              # 不知道体检谁 → null,不是 []
        entry = body["degraded"][0]
        assert entry["ds"] is None
        assert entry["error"] == "RuntimeError"


class TestCoverageAuth:
    async def test_non_admin_403_anon_401(self, user_client, anon_client):
        assert (await user_client.get("/v1/admin/coverage")).status_code == 403
        r = await anon_client.get("/v1/admin/coverage")
        assert r.status_code in (401, 403)
