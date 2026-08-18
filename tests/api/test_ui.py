"""Web UI static serving (trove/api/static/ mounted at /ui) and packaging.

The frontend itself is plain HTML/JS with no test infra — these tests
pin the serving contract (mount, redirect, content) and the packaging
contract (package-data glob ships the files).
"""

from importlib.resources import files
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


class TestStatic:
    async def test_index_served(self, client):
        resp = await client.get("/ui/")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert 'id="question-input"' in resp.text

    async def test_root_redirects_to_ui(self, client):
        resp = await client.get("/", follow_redirects=False)
        assert resp.status_code in (307, 308)
        assert resp.headers["location"].rstrip("/").endswith("/ui")

    async def test_root_redirect_followed(self, client):
        resp = await client.get("/", follow_redirects=True)
        assert resp.status_code == 200
        assert resp.url.path == "/ui/"

    async def test_app_js_served(self, client):
        resp = await client.get("/ui/app.js")
        assert resp.status_code == 200
        assert "sendQuestion" in resp.text

    async def test_static_no_cache(self, client):
        # The UI is updated in place; browsers must revalidate on every
        # load instead of mixing a cached old page with new assets.
        resp = await client.get("/ui/app.js")
        assert resp.headers["cache-control"] == "no-cache"

    async def test_style_css_served(self, client):
        resp = await client.get("/ui/style.css")
        assert resp.status_code == 200
        assert "text/css" in resp.headers["content-type"]

    async def test_unknown_static_404(self, client):
        assert (await client.get("/ui/nope.js")).status_code == 404


class TestPackageData:
    def test_static_files_ship_in_package(self):
        pkg = files("trove.api") / "static"
        for name in ("index.html", "app.js", "style.css"):
            assert (pkg / name).is_file(), f"trove/api/static/{name} missing"

    def test_pyproject_declares_static_glob(self):
        # A source-tree run cannot detect a forgotten package-data entry;
        # the pyproject text check is what guards the wheel/sdist.
        text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        assert "api/static/*" in text
