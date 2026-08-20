"""FastAPI application factory.

`create_app(components)` wraps the dict returned by
`main.create_app_components` (session_manager, catalog_service,
connector_registry, kb, ...) into a FastAPI app. Each component is
exposed on `app.state.<name>` for the routers; tests build the
components dict with mocks.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from trove.api.routers import catalog, chat, kb
from trove.core.errors import DatasourceError, SessionError
from trove.core.logging import get_logger

logger = get_logger(__name__)


def _static_dir() -> str | None:
    """Locate trove/api/static via importlib.resources (falls back to __file__).

    Returns None when the static files are missing from the install —
    the API stays bootable and only /ui/* 404s.
    """
    try:
        return str(resources.files("trove.api") / "static")
    except (OSError, TypeError):
        candidate = Path(__file__).resolve().parent / "static"
        return str(candidate) if candidate.is_dir() else None


class _NoCacheStaticFiles(StaticFiles):
    """StaticFiles with Cache-Control: no-cache.

    The UI is updated in place during development; without this header
    browsers keep serving a stale index.html/app.js from cache after an
    update, mixing old DOM structure with new scripts and breaking the page.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


def create_app(components: dict) -> FastAPI:
    """Build the FastAPI app from a components dict (see main.py)."""
    app = FastAPI(title="Trove API", version="0.1.0", docs_url="/v1/docs")
    for name, value in components.items():
        setattr(app.state, name, value)

    # Auth: real service → mount /v1/auth (+ /v1/admin when present);
    # missing → NullAuth fallback so embedded/stray callers keep working
    # (every request runs as synthetic local admin, loud one-time warning).
    from trove.api.deps import NullAuth
    from trove.api.routers import admin as admin_router
    from trove.api.routers import auth as auth_router

    auth = components.get("auth")
    if auth is None:
        auth = NullAuth()
        app.state.auth = auth
        logger.warning(
            "AUTH DISABLED — no auth service in components; "
            "requests run as synthetic local admin"
        )
    if not isinstance(auth, NullAuth):
        app.include_router(auth_router.router, prefix="/v1")
        app.include_router(admin_router.router, prefix="/v1")

    app.include_router(chat.router, prefix="/v1")
    app.include_router(catalog.router, prefix="/v1")
    app.include_router(kb.router, prefix="/v1")

    static_dir = _static_dir()
    if static_dir is not None:
        app.mount(
            "/ui",
            _NoCacheStaticFiles(directory=static_dir, html=True, check_dir=False),
            name="ui",
        )

    @app.get("/", include_in_schema=False)
    async def index_redirect() -> RedirectResponse:
        return RedirectResponse(url="/ui/", status_code=307)

    @app.exception_handler(SessionError)
    async def _session_error(request: Request, exc: SessionError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(DatasourceError)
    async def _datasource_error(request: Request, exc: DatasourceError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.get("/v1/health")
    async def health() -> dict:
        return {"status": "ok"}

    return app
