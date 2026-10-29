"""Response shapes the API and MCP share: users named, never by id, and one error body."""

from collections.abc import Mapping, Sequence
from typing import Any

from sqlmodel import Session

from barrelvault.account import user_refs
from barrelvault.api.schemas import (
    BlockOut,
    DuplicateTaskOut,
    FollowUpOut,
    HistoryOut,
    TaskEntryOut,
    TaskOut,
    UserOut,
)
from barrelvault.blocks import BlockView, FollowUp
from barrelvault.errors import (
    Conflict,
    Duplicate,
    Forbidden,
    Invalid,
    NotFound,
    Refused,
    ServiceError,
)
from barrelvault.tasks import HistoryEntry, TaskDetail, TaskSummary

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


def error_body(error: ServiceError) -> tuple[int, dict[str, Any]]:
    """The HTTP status and the {"error", "message"} body for a refusal."""
    status, kind = next((s, k) for cls, s, k in _KINDS if isinstance(error, cls))
    body: dict[str, Any] = {"error": kind, "message": error.message}
    if isinstance(error, Duplicate):
        task = DuplicateTaskOut(
            id=error.task_id, title=error.title, status=error.status, due_at=error.due_at
        )
        body["task"] = task.model_dump(mode="json")
    return status, body


def invalid_body(errors: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The invalid body for refused input, naming the first field pydantic refused."""
    first = errors[0]
    where = ".".join(str(part) for part in first["loc"] if part != "body")
    return {"error": "invalid", "message": f"{where}: {first['msg']}" if where else first["msg"]}


def named(session: Session, user_ids: set[int]) -> dict[int, UserOut]:
    refs = user_refs(session, user_ids)
    return {user_id: UserOut(name=ref.name, kind=ref.kind) for user_id, ref in refs.items()}


def task_out(session: Session, task: TaskDetail) -> TaskOut:
    names = named(session, {task.created_by, task.updated_by})
    fields = dict(vars(task))
    fields["created_by"], fields["updated_by"] = names[task.created_by], names[task.updated_by]
    return TaskOut(**fields)


def entries_out(session: Session, tasks: list[TaskSummary]) -> list[TaskEntryOut]:
    names = named(session, {t.created_by for t in tasks} | {t.updated_by for t in tasks})
    out = []
    for task in tasks:
        fields = dict(vars(task))
        fields["created_by"], fields["updated_by"] = names[task.created_by], names[task.updated_by]
        out.append(TaskEntryOut(**fields))
    return out


def blocks_out(session: Session, blocks: list[BlockView]) -> list[BlockOut]:
    ids = {b.created_by for b in blocks} | {b.released_by for b in blocks if b.released_by}
    names = named(session, ids)
    out = []
    for block in blocks:
        fields = dict(vars(block))
        fields["created_by"] = names[block.created_by]
        fields["released_by"] = names[block.released_by] if block.released_by else None
        out.append(BlockOut(**fields))
    return out


def follow_ups_out(session: Session, follow_ups: list[FollowUp]) -> list[FollowUpOut]:
    blocks = blocks_out(session, [follow_up.block for follow_up in follow_ups])
    return [
        FollowUpOut(**block.model_dump(), task_title=follow_up.task_title)
        for block, follow_up in zip(blocks, follow_ups, strict=True)
    ]


def history_out(session: Session, entries: list[HistoryEntry]) -> list[HistoryOut]:
    names = named(session, {entry.user_id for entry in entries})
    return [
        HistoryOut(editor=names[entry.user_id], created_at=entry.created_at, state=entry.state)
        for entry in entries
    ]
