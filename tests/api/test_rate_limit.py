"""check_api_rate 依赖:按 user 的令牌桶 + 日配额,命中 → 429。"""

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from trove.api.deps import check_api_rate
from trove.core.config import AgentConfig
from trove.services.ratelimit import RateLimiter, reset_rate_limiter


def _build_app(config: AgentConfig) -> FastAPI:
    app = FastAPI()
    from trove.api.deps import NullAuth

    app.state.rate_limiter = RateLimiter()
    app.state.config = config
    app.state.auth = NullAuth()

    @app.post("/x")
    async def x(_rate: None = Depends(check_api_rate)):
        return {"ok": True}

    return app


async def _post(app: FastAPI) -> int:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as c:
        resp = await c.post("/x")
        return resp.status_code


async def test_per_minute_limit_returns_429():
    app = _build_app(AgentConfig(api_rate_per_minute=2, api_daily_quota=0))
    try:
        assert await _post(app) == 200
        assert await _post(app) == 200
        resp_status = await _post(app)
        assert resp_status == 429
        assert (await AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ).post("/x")).status_code == 429
    finally:
        reset_rate_limiter(app.state.rate_limiter)


async def test_daily_quota_returns_429():
    app = _build_app(AgentConfig(api_rate_per_minute=0, api_daily_quota=2))
    try:
        assert await _post(app) == 200
        assert await _post(app) == 200
        assert await _post(app) == 429
    finally:
        reset_rate_limiter(app.state.rate_limiter)


async def test_disabled_when_zero():
    app = _build_app(AgentConfig(api_rate_per_minute=0, api_daily_quota=0))
    try:
        for _ in range(5):
            assert await _post(app) == 200
    finally:
        reset_rate_limiter(app.state.rate_limiter)


async def test_missing_limiter_skips():
    app = _build_app(AgentConfig(api_rate_per_minute=1))
    app.state.rate_limiter = None
    assert await _post(app) == 200
