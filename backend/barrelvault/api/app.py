"""The FastAPI app: the API under /api, the MCP server at /mcp, and one error body."""

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

import anyio
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from barrelvault.api import routes, session_routes
from barrelvault.api.deps import SAFE_METHODS
from barrelvault.api.shapes import error_body, invalid_body
from barrelvault.db import use_database
from barrelvault.errors import ServiceError
from barrelvault.mcp_server import build_mcp, mcp_app
from barrelvault.settings import Settings, load_settings
from barrelvault.surface import DuplicateWarnings

# Well below the 40 worker threads a request's sync code runs on.
MAX_WRITES_ADMITTED = 10


class _WriteAdmission:
    """Hold writes past a limit on the event loop, where waiting takes no worker thread.

    A write holds the database lock across several hops onto a worker thread. If waiting
    writers filled every thread, the one holding the lock could not finish, and they would
    all run out the busy timeout. MCP calls are all POSTs, so its reads count here too.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app
        self._admitted = anyio.Semaphore(MAX_WRITES_ADMITTED)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] in SAFE_METHODS:
            await self._app(scope, receive, send)
            return
        async with self._admitted:
            await self._app(scope, receive, send)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app: the API under /api and the MCP server at /mcp.

    Settings load here, once, when the app starts.
    """
    settings = settings or load_settings()
    use_database(settings.db_path)
    # The duplicate warnings the API and MCP share, kept for this process's life.
    warnings = DuplicateWarnings()
    server = build_mcp(warnings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        async with server.session_manager.run():
            yield

    app = FastAPI(title="Barrelvault", lifespan=lifespan)
    app.state.settings = settings
    app.state.duplicate_warnings = warnings
    app.include_router(session_routes.router, prefix="/api")
    app.include_router(routes.router, prefix="/api")
    app.router.routes.append(Route("/mcp", mcp_app(server)))
    app.add_middleware(_WriteAdmission)
    app.add_exception_handler(ServiceError, _service_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    return app


def _error(
    status: int, kind: str, message: str, headers: Mapping[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"error": kind, "message": message}, headers=headers
    )


async def _service_error(_request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, ServiceError)
    status, body = error_body(error)
    return JSONResponse(status_code=status, content=body)


async def _validation_error(_request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, RequestValidationError)
    return JSONResponse(status_code=422, content=invalid_body(error.errors()))


async def _http_error(_request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, StarletteHTTPException)
    kind = "not_found" if error.status_code == 404 else "invalid"
    # A 405 carries the Allow header the error was raised with.
    return _error(error.status_code, kind, str(error.detail).lower(), headers=error.headers)
