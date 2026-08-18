"""FastAPI application factory.

`create_app(components)` wraps the dict returned by
`main.create_app_components` (session_manager, catalog_service,
connector_registry, kb, ...) into a FastAPI app. Each component is
exposed on `app.state.<name>` for the routers; tests build the
components dict with mocks.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from trove.api.routers import catalog, chat, kb
from trove.core.errors import DatasourceError, SessionError


def create_app(components: dict) -> FastAPI:
    """Build the FastAPI app from a components dict (see main.py)."""
    app = FastAPI(title="Trove API", version="0.1.0", docs_url="/v1/docs")
    for name, value in components.items():
        setattr(app.state, name, value)

    app.include_router(chat.router, prefix="/v1")
    app.include_router(catalog.router, prefix="/v1")
    app.include_router(kb.router, prefix="/v1")

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
