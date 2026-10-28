"""Task create, read, update, and delete. Each call takes the acting user and reads no session.

A call acts in the actor's account, the person's own or an agent's owner's, and a task
in another account is not found. Reads return a frozen view, so a caller never sees a
null text column.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import select as sql_select
from sqlmodel import Session, col, select

from barrelvault.account import account_of, actor_id
from barrelvault.errors import Conflict, Invalid, NotFound
from barrelvault.models import Block, Task, TaskHistory, User
from barrelvault.status import STATUSES, TODO
from barrelvault.tags import read_tags, stored_tags
from barrelvault.times import as_utc, now, to_utc
from barrelvault.tree import Tree

# A field the caller did not send. An update leaves it as stored.
UNSET: Final[Any] = object()

ESTIMATE_MAX = 32767


@dataclass(frozen=True)
class TaskView:
    id: int
    parent_id: int | None
    title: str
    notes: str
    tags: list[str]
    status: int
    estimate_minutes: int | None
    due_at: datetime | None
    created_by: int
    created_at: datetime
    updated_by: int
    updated_at: datetime
    owned_by: int


@dataclass(frozen=True)
class HistoryEntry:
    user_id: int
    created_at: datetime
    state: dict[str, Any]


def create_task(
    session: Session,
    actor: User,
    title: str,
    *,
    parent_id: int | None = None,
    notes: str | None = None,
    tags: str | None = None,
    status: int = TODO,
    estimate_minutes: int | None = None,
    due_at: datetime | None = None,
) -> TaskView:
    owner = account_of(session, actor)
    if parent_id is not None:
        task_in_account(session, owner.user_id, parent_id)
    moment = now()
    task = Task(
        parent_id=parent_id,
        title=normalize_title(title),
        notes=notes,
        tags=stored_tags(tags),
        status=_check_status(status),
        estimate_minutes=_check_estimate(estimate_minutes),
        due_at=to_utc(due_at, owner.timezone) if due_at is not None else None,
        created_by=actor_id(actor),
        created_at=moment,
        updated_by=actor_id(actor),
        updated_at=moment,
        owned_by=owner.user_id,
    )
    session.add(task)
    session.flush()
    return _view(task)


def get_task(session: Session, actor: User, task_id: int) -> TaskView:
    owner = account_of(session, actor)
    return _view(task_in_account(session, owner.user_id, task_id))


def list_tasks(session: Session, actor: User, parent_id: int | None = None) -> list[TaskView]:
    """One level: the roots, or the direct children of a parent, by created_at then id."""
    owner = account_of(session, actor)
    if parent_id is not None:
        task_in_account(session, owner.user_id, parent_id)
    query = (
        select(Task)
        .where(Task.owned_by == owner.user_id, Task.parent_id == parent_id)
        .order_by(col(Task.created_at), col(Task.id))
    )
    return [_view(task) for task in session.exec(query)]


def update_task(
    session: Session,
    actor: User,
    task_id: int,
    *,
    title: Any = UNSET,
    notes: Any = UNSET,
    tags: Any = UNSET,
    status: Any = UNSET,
    estimate_minutes: Any = UNSET,
    due_at: Any = UNSET,
    parent_id: Any = UNSET,
) -> TaskView:
    """Change only the fields sent. A change that leaves the task as it was writes nothing."""
    owner = account_of(session, actor)
    task = task_in_account(session, owner.user_id, task_id)
    changes: dict[str, Any] = {}
    if title is not UNSET:
        changes["title"] = normalize_title(title)
    if notes is not UNSET:
        changes["notes"] = notes
    if tags is not UNSET:
        changes["tags"] = stored_tags(tags)
    if status is not UNSET:
        changes["status"] = _check_status(status)
    if estimate_minutes is not UNSET:
        changes["estimate_minutes"] = _check_estimate(estimate_minutes)
    if due_at is not UNSET:
        changes["due_at"] = to_utc(due_at, owner.timezone) if due_at is not None else None
    if parent_id is not UNSET:
        _check_move(session, owner.user_id, task, parent_id)
        changes["parent_id"] = parent_id

    changed = {
        name: value for name, value in changes.items() if not _same(getattr(task, name), value)
    }
    if not changed:
        return _view(task)
    if "status" in changed and _has_open_block(session, task):
        raise Conflict("task is blocked")
    for name, value in changed.items():
        setattr(task, name, value)
    touch(task, actor)
    session.add(task)
    session.flush()
    return _view(task)


def delete_task(session: Session, actor: User, task_id: int) -> None:
    """Delete the task. The cascade takes its subtree, blocks, and history."""
    owner = account_of(session, actor)
    task = task_in_account(session, owner.user_id, task_id)
    gone = {task_id} | _descendant_ids(session, owner.user_id, task_id)
    session.delete(task)
    session.flush()
    # The database cascade removed these rows, so the session must stop handing them out.
    for obj in list(session.identity_map.values()):
        on_task = obj.id if isinstance(obj, Task) else getattr(obj, "task_id", None)
        if isinstance(obj, Task | Block | TaskHistory) and on_task in gone:
            session.expunge(obj)


def task_history(session: Session, actor: User, task_id: int) -> list[HistoryEntry]:
    """The task's snapshots, ordered by created_at then id."""
    owner = account_of(session, actor)
    task_in_account(session, owner.user_id, task_id)
    query = (
        select(TaskHistory)
        .where(TaskHistory.task_id == task_id)
        .order_by(col(TaskHistory.created_at), col(TaskHistory.id))
    )
    return [
        HistoryEntry(row.user_id, as_utc(row.created_at), row.state) for row in session.exec(query)
    ]


def touch(task: Task, actor: User) -> None:
    """Mark the task changed by the actor. A block change calls this, and it writes no snapshot.

    Stored times are whole milliseconds, so two changes inside one millisecond would
    share a time. updated_at moves at least one millisecond past the last change.
    """
    task.updated_at = max(now(), as_utc(task.updated_at) + timedelta(milliseconds=1))
    task.updated_by = actor_id(actor)


def task_in_account(session: Session, account_id: int, task_id: int) -> Task:
    """A task in the account. A task in another account is not found."""
    task = session.exec(
        select(Task).where(Task.id == task_id, Task.owned_by == account_id)
    ).one_or_none()
    if task is None:
        raise NotFound("no such task")
    return task


def normalize_title(title: str) -> str:
    """The stored form of a title: trimmed, each run of whitespace one space."""
    clean = " ".join((title or "").split())
    if not clean:
        raise Invalid("title is required")
    return clean


def _parents(session: Session, account_id: int) -> Tree:
    """Every task's parent in the account, from one query."""
    rows = session.execute(
        sql_select(col(Task.id), col(Task.parent_id)).where(col(Task.owned_by) == account_id)
    ).all()
    return Tree({row[0]: row[1] for row in rows})


def _check_move(session: Session, account_id: int, task: Task, parent_id: int | None) -> None:
    if parent_id is None:
        return
    tree = _parents(session, account_id)
    if parent_id not in tree:
        raise NotFound("no such parent")
    assert task.id is not None
    if tree.would_cycle(task.id, parent_id):
        raise Invalid("a task cannot be nested under itself or a task under it")


def _descendant_ids(session: Session, account_id: int, task_id: int) -> set[int]:
    tree = _parents(session, account_id)
    return {node for node in tree if task_id in tree.ancestors(node)}


def _has_open_block(session: Session, task: Task) -> bool:
    query = select(Block.id).where(Block.task_id == task.id, col(Block.released_at).is_(None))
    return session.exec(query.limit(1)).first() is not None


def _same(stored: Any, sent: Any) -> bool:
    """A null text column reads as "", so it equals an empty string."""
    if isinstance(stored, str | None) and isinstance(sent, str | None):
        return (stored or "") == (sent or "")
    if isinstance(stored, datetime) and isinstance(sent, datetime):
        return as_utc(stored) == as_utc(sent)
    return bool(stored == sent)


def _check_status(status: int) -> int:
    if type(status) is not int or status not in STATUSES:
        raise Invalid("status is 0, 1, 2, or 3")
    return status


def _check_estimate(estimate_minutes: int | None) -> int | None:
    if estimate_minutes is None:
        return None
    if type(estimate_minutes) is not int or not 0 <= estimate_minutes <= ESTIMATE_MAX:
        raise Invalid(f"estimate_minutes is from 0 through {ESTIMATE_MAX}")
    return estimate_minutes


def _view(task: Task) -> TaskView:
    assert task.id is not None
    return TaskView(
        id=task.id,
        parent_id=task.parent_id,
        title=task.title,
        notes=task.notes or "",
        tags=read_tags(task.tags),
        status=task.status,
        estimate_minutes=task.estimate_minutes,
        due_at=as_utc(task.due_at) if task.due_at is not None else None,
        created_by=task.created_by,
        created_at=as_utc(task.created_at),
        updated_by=task.updated_by,
        updated_at=as_utc(task.updated_at),
        owned_by=task.owned_by,
    )
