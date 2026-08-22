"""Frontend/backend decoupling (frontend served by CDN/nginx, not the API).

The backend never serves the SPA: /ui/* and the root redirect were removed
so the API is a pure JSON service (every path lives under /v1).
"""

from __future__ import annotations


class TestUiDecoupled:
    async def test_ui_not_served(self, client):
        resp = await client.get("/ui/")
        assert resp.status_code == 404

    async def test_root_not_redirecting_to_ui(self, client):
        resp = await client.get("/")
        assert resp.status_code == 404