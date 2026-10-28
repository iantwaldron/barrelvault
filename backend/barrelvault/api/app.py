"""The FastAPI app: the API under /api, the MCP server at /mcp, and one error body."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import Route

from barrelvault.api import routes, session_routes
from barrelvault.api.shapes import error_body
from barrelvault.errors import ServiceError
from barrelvault.mcp_server import build_mcp, mcp_app
from barrelvault.settings import Settings, load_settings
from barrelvault.surface import DuplicateWarnings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app: the API under /api and the MCP server at /mcp.

    Settings load here, once, when the app starts.
    """
    # The duplicate warnings the API and MCP share, kept for this process's life.
    warnings = DuplicateWarnings()
    server = build_mcp(warnings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        async with server.session_manager.run():
            yield

    app = FastAPI(title="Barrelvault", lifespan=lifespan)
    app.state.settings = settings or load_settings()
    app.state.duplicate_warnings = warnings
    app.state.mcp_server = server
    app.include_router(session_routes.router, prefix="/api")
    app.include_router(routes.router, prefix="/api")
    app.router.routes.append(Route("/mcp", mcp_app(server)))
    app.add_exception_handler(ServiceError, _service_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    return app


def _error(status: int, kind: str, message: str, **extra: object) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": kind, "message": message, **extra})


async def _service_error(_request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, ServiceError)
    status, body = error_body(error)
    return JSONResponse(status_code=status, content=body)


async def _validation_error(_request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, RequestValidationError)
    first = error.errors()[0]
    where = ".".join(str(part) for part in first["loc"] if part != "body")
    return _error(422, "invalid", f"{where}: {first['msg']}" if where else first["msg"])


async def _http_error(_request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, StarletteHTTPException)
    kind = "not_found" if error.status_code == 404 else "invalid"
    return _error(error.status_code, kind, str(error.detail).lower())
