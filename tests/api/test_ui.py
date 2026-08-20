"""Web UI static serving (Vue/Vite build mounted at /ui) and packaging.

The frontend is built with Vite (frontend/ → trove/api/static/); these
tests pin the serving contract (mount, redirect, real asset paths,
no-cache) and the packaging contract (package-data glob ships the files
including the hashed assets/ bundle).
"""

from __future__ import annotations

import re
from importlib.resources import files
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

INDEX_ASSET_RE = re.compile(r'<script[^>]+src="([^"]+)"')


def _asset_paths(index_html: str) -> list[str]:
    """Extract /ui/ asset URLs referenced by the built index.html."""
    return [m for m in INDEX_ASSET_RE.findall(index_html) if m.startswith("/ui/")]


class TestStatic:
    async def test_index_served(self, client):
        resp = await client.get("/ui/")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        # Vue mount point
        assert 'id="app"' in resp.text

    async def test_root_redirects_to_ui(self, client):
        resp = await client.get("/", follow_redirects=False)
        assert resp.status_code in (307, 308)
        assert resp.headers["location"].rstrip("/").endswith("/ui")

    async def test_root_redirect_followed(self, client):
        resp = await client.get("/", follow_redirects=True)
        assert resp.status_code == 200
        assert resp.url.path == "/ui/"

    async def test_bundled_js_served_with_marker(self, client):
        """The entry bundle must exist and carry the __TROVE_UI__ marker
        (set in frontend/src/main.ts) — guards against committing a stale
        build that predates the current source."""
        resp = await client.get("/ui/")
        assets = _asset_paths(resp.text)
        assert assets, "no /ui/ script assets in built index.html"
        js = await client.get(assets[0])
        assert js.status_code == 200
        assert "javascript" in js.headers["content-type"]
        assert "__TROVE_UI__" in js.text

    async def test_static_no_cache(self, client):
        resp = await client.get("/ui/")
        assets = _asset_paths(resp.text)
        assert assets
        resp = await client.get(assets[0])
        assert resp.headers["cache-control"] == "no-cache"

    async def test_css_served(self, client):
        resp = await client.get("/ui/")
        css_urls = re.findall(r'<link[^>]+href="(/ui/[^"]+\.css)"', resp.text)
        assert css_urls, "no css assets in built index.html"
        resp = await client.get(css_urls[0])
        assert resp.status_code == 200
        assert "text/css" in resp.headers["content-type"]

    async def test_unknown_static_404(self, client):
        assert (await client.get("/ui/nope.js")).status_code == 404


class TestPackageData:
    def test_static_files_ship_in_package(self):
        pkg = files("trove.api") / "static"
        assert (pkg / "index.html").is_file(), "trove/api/static/index.html missing"
        assets = pkg / "assets"
        assert assets.is_dir() and any(assets.iterdir()), "trove/api/static/assets/ empty"

    def test_pyproject_declares_static_glob(self):
        # A source-tree run cannot detect a forgotten package-data entry;
        # the pyproject text check is what guards the wheel/sdist.
        text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        assert "api/static/*" in text
        assert "api/static/assets/*" in text
