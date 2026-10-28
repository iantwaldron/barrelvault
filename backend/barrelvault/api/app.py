"""The FastAPI app: the routes under /api, and one error body for every refusal."""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from barrelvault.api import routes, session_routes
from barrelvault.api.schemas import DuplicateTaskOut
from barrelvault.errors import (
    Conflict,
    Duplicate,
    Forbidden,
    Invalid,
    NotFound,
    Refused,
    ServiceError,
)
from barrelvault.settings import Settings, load_settings
from barrelvault.surface import DuplicateWarnings

# Each kind of refusal, its status, and the name its body carries. The first match wins,
# so Duplicate comes before Conflict, the kind it extends.
_KINDS: tuple[tuple[type[ServiceError], int, str], ...] = (
    (Refused, 401, "refused"),
    (Forbidden, 403, "forbidden"),
    (NotFound, 404, "not_found"),
    (Invalid, 422, "invalid"),
    (Duplicate, 409, "duplicate"),
    (Conflict, 409, "conflict"),
)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app. Settings load here, once, when the app starts."""
    app = FastAPI(title="Barrelvault")
    app.state.settings = settings or load_settings()
    # The duplicate warnings the API shares with the MCP, kept for this process's life.
    app.state.duplicate_warnings = DuplicateWarnings()
    app.include_router(session_routes.router, prefix="/api")
    app.include_router(routes.router, prefix="/api")
    app.add_exception_handler(ServiceError, _service_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    return app


def _error(status: int, kind: str, message: str, **extra: object) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": kind, "message": message, **extra})


async def _service_error(_request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, ServiceError)
    status, kind = next((s, k) for cls, s, k in _KINDS if isinstance(error, cls))
    if isinstance(error, Duplicate):
        task = DuplicateTaskOut(
            id=error.task_id, title=error.title, status=error.status, due_at=error.due_at
        )
        return _error(status, kind, error.message, task=task.model_dump(mode="json"))
    return _error(status, kind, error.message)


async def _validation_error(_request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, RequestValidationError)
    first = error.errors()[0]
    where = ".".join(str(part) for part in first["loc"] if part != "body")
    return _error(422, "invalid", f"{where}: {first['msg']}" if where else first["msg"])


async def _http_error(_request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, StarletteHTTPException)
    kind = "not_found" if error.status_code == 404 else "invalid"
    return _error(error.status_code, kind, str(error.detail).lower())
