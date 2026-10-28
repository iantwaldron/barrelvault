"""The REST request and response models. FastAPI builds the OpenAPI schema from these."""

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, PlainSerializer, StrictInt


def _utc_text(moment: datetime) -> str:
    """ISO 8601 in UTC with milliseconds and a Z, such as 2026-11-06T23:00:00.000Z."""
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


UtcTime = Annotated[datetime, PlainSerializer(_utc_text, return_type=str)]


class Body(BaseModel):
    """A request body. An unknown field is refused, so a misspelled field is not lost."""

    model_config = ConfigDict(extra="forbid")


class UserOut(BaseModel):
    name: str
    kind: Literal["person", "agent"]


class ErrorOut(BaseModel):
    error: str
    message: str


class DuplicateTaskOut(BaseModel):
    id: int
    title: str
    status: int
    due_at: UtcTime | None


class DuplicateOut(ErrorOut):
    task: DuplicateTaskOut


class LoginIn(Body):
    email: str
    password: str


class ExchangeIn(Body):
    key: str


class TokenOut(BaseModel):
    token: str
    expires_at: UtcTime


class AccountOut(BaseModel):
    name: str
    kind: Literal["person", "agent"]
    email: str
    timezone: str


class AccountPatch(Body):
    timezone: str


class AgentIn(Body):
    name: str


class AgentOut(BaseModel):
    name: str
    retired_at: UtcTime | None
    key_expires_at: UtcTime | None


class KeyOut(BaseModel):
    key: str
    key_expires_at: UtcTime


class TaskOut(BaseModel):
    id: int
    parent_id: int | None
    title: str
    notes: str
    tags: list[str]
    status: int
    estimate_minutes: int | None
    due_at: UtcTime | None
    created_by: UserOut
    created_at: UtcTime
    updated_by: UserOut
    updated_at: UtcTime
    effectively_dropped: bool
    blocked: bool
    dropped_reason: str


class TaskEntryOut(BaseModel):
    """A task in a list or tree: no notes or dropped_reason, and whether it matched."""

    id: int
    parent_id: int | None
    title: str
    tags: list[str]
    status: int
    estimate_minutes: int | None
    due_at: UtcTime | None
    created_by: UserOut
    created_at: UtcTime
    updated_by: UserOut
    updated_at: UtcTime
    effectively_dropped: bool
    blocked: bool
    matched: bool


class TaskCreate(Body):
    title: str
    parent_id: StrictInt | None = None
    notes: str | None = None
    tags: str | None = None
    status: StrictInt = 0
    estimate_minutes: StrictInt | None = None
    due_at: datetime | None = None
    allow_duplicate: bool = False


class TaskPatch(Body):
    """Only the fields sent change. A field sent as null is cleared."""

    title: str | None = None
    notes: str | None = None
    tags: str | None = None
    status: StrictInt | None = None
    estimate_minutes: StrictInt | None = None
    due_at: datetime | None = None
    parent_id: StrictInt | None = None
    append_notes: str | None = None
    add_tags: str | None = None
    remove_tags: str | None = None
    release_blocks: bool = False
    expected_updated_at: datetime | None = None


class DropIn(Body):
    reason: str | None = None
    release_blocks: bool = False
    expected_updated_at: datetime | None = None


class HistoryOut(BaseModel):
    editor: UserOut
    created_at: UtcTime
    state: dict[str, Any]


class BlockIn(Body):
    reason: str


class BlockOut(BaseModel):
    id: int
    task_id: int
    reason: str
    created_by: UserOut
    created_at: UtcTime
    released_by: UserOut | None
    released_at: UtcTime | None
