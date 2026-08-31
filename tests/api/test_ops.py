"""Ops endpoints: real health checks, X-Request-ID, Prometheus metrics."""

import pytest

from trove.core.errors import SQLExecutionError
from trove.core.metrics import (
    record_llm_call,
    record_sql,
    render_metrics,
)


class TestHealth:
    async def test_health_ok(self, client):
        resp = await client.get("/v1/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["checks"]["storage"]["ok"] is True
        # sqlite_registry fixture: one connected in-memory adapter
        assert body["checks"]["datasources"] == {"test_db": {"ok": True}}
        # mock gateway has no providers — reported, not pinged
        assert body["checks"]["llm"] == {"configured": False, "providers": 0}

    async def test_health_storage_down_503(self, api_app, anon_client, monkeypatch):
        class DeadBackend:
            async def execute(self, sql, params=()):
                raise RuntimeError("boom")

        class DeadStore:
            backend = DeadBackend()

        # monkeypatch:测后自动恢复,不污染同文件后续用例
        monkeypatch.setattr(
            api_app.state, "session_store", DeadStore(), raising=False
        )
        resp = await anon_client.get("/v1/health")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "unavailable"
        # 只报类型名,不回传驱动原文(凭据/主机信息不进响应)
        assert body["checks"]["storage"] == {"ok": False, "error": "RuntimeError"}

    async def test_health_degraded_datasource(self, api_app, anon_client, monkeypatch):
        class BrokenAdapter:
            name = "broken"
            is_connected = True

            async def execute(self, sql):
                raise SQLExecutionError(message="down", sql=sql)

            async def disconnect(self):
                pass  # close_all 的 teardown 路径需要

        monkeypatch.setitem(
            api_app.state.connector_registry._adapters, "broken", BrokenAdapter()
        )
        resp = await anon_client.get("/v1/health")
        assert resp.status_code == 200  # 进程活着,数据源坏了是 degraded
        body = resp.json()
        assert body["status"] == "degraded"
        assert body["checks"]["datasources"]["broken"]["ok"] is False
        assert body["checks"]["datasources"]["test_db"]["ok"] is True


class TestRequestId:
    async def test_echoes_provided_id(self, anon_client):
        resp = await anon_client.get(
            "/v1/health", headers={"X-Request-ID": "trace-abc.123"}
        )
        assert resp.headers["x-request-id"] == "trace-abc.123"

    async def test_generates_uuid_when_absent(self, anon_client):
        resp = await anon_client.get("/v1/health")
        rid = resp.headers["x-request-id"]
        assert len(rid) == 32  # uuid4 hex

    async def test_rejects_unsafe_id(self, anon_client):
        resp = await anon_client.get(
            "/v1/health", headers={"X-Request-ID": "bad id; rm -rf"}
        )
        assert resp.headers["x-request-id"] != "bad id; rm -rf"

    async def test_log_filter_defaults_outside_request(self):
        import logging

        from trove.core.request_id import RequestIdFilter

        record = logging.LogRecord(
            "test", logging.INFO, __file__, 1, "msg", (), None
        )
        assert RequestIdFilter().filter(record) is True
        assert record.request_id == "-"


class TestMetrics:
    async def test_metrics_endpoint(self, anon_client):
        # 先产生一条已完成的请求;本次 scrape 自身的计数在响应
        # 渲染之后才落账(拉模式监控的正常语义,下轮可见)
        await anon_client.get("/v1/health")
        resp = await anon_client.get("/v1/metrics")
        assert resp.status_code == 200
        assert "text/plain" in resp.headers["content-type"]
        assert "trove_http_requests_total" in resp.text
        assert 'path="/v1/health"' in resp.text

    async def test_middleware_counts_requests(self, anon_client):
        await anon_client.get("/v1/health")
        resp = await anon_client.get("/v1/metrics")
        assert 'path="/v1/health"' in resp.text
        assert 'path="/v1/metrics"' in resp.text

    def test_record_helpers_land_in_exposition(self):
        record_llm_call("openai/gpt-4o", "success", 0.1)
        record_sql("test_db", "error", 0.2)
        text = render_metrics().decode()
        assert "trove_llm_calls_total" in text
        assert 'provider="openai"' in text
        assert "trove_sql_queries_total" in text
        assert 'datasource="test_db"' in text


@pytest.mark.parametrize("route", ["/v1/health", "/v1/metrics"])
async def test_ops_routes_open_unauthenticated(anon_client, route):
    """health/metrics 免鉴权(编排器依赖),但不能泄露敏感数据。"""
    resp = await anon_client.get(route)
    assert resp.status_code == 200
