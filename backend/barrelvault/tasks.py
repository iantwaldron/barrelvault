"""Task reads and writes. Each call takes the acting user and reads no session.

A call acts in the actor's account, the person's own or an agent's owner's, and a
task in another account is not found. Reads return frozen views, so a caller never
sees a null text column, and they carry the effectively dropped and blocked states
the tree works out.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import select as sql_select
from sqlmodel import Session, col, select

from barrelvault.account import account_of, actor_id
from barrelvault.errors import Conflict, Duplicate, Invalid, NotFound
from barrelvault.models import Block, Task, TaskHistory, User, title_key
from barrelvault.notes import append_note, close_open_fence, dropped_block, dropped_reason
from barrelvault.status import CLOSED, DROPPED, OPEN, STATUSES, TODO
from barrelvault.tags import add_tags, has_tags, read_tags, remove_tags, stored_tags
from barrelvault.times import as_utc, now, to_utc
from barrelvault.tree import TaskRow, Tree

# A field the caller did not send. An update leaves it as stored.
UNSET: Final[Any] = object()

ESTIMATE_MAX = 32767


@dataclass(frozen=True)
class TaskSummary:
    """A task in a list or tree. notes is left out, since a list never loads it."""

    id: int
    parent_id: int | None
    title: str
    tags: list[str]
    status: int
    estimate_minutes: int | None
    due_at: datetime | None
    created_by: int
    created_at: datetime
    updated_by: int
    updated_at: datetime
    effectively_dropped: bool
    blocked: bool
    # False for an ancestor a filtered tree includes only so the match keeps its parents.
    matched: bool


@dataclass(frozen=True)
class TaskDetail:
    """One task with everything a read of it returns."""

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
    effectively_dropped: bool
    blocked: bool
    dropped_reason: str


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
    allow_duplicate: bool = False,
) -> TaskDetail:
    """Create a task in the actor's account. A likely duplicate is refused unless allowed."""
    owner = account_of(session, actor)
    clean_title = normalize_title(title)
    stored_due = to_utc(due_at, owner.timezone) if due_at is not None else None
    if parent_id is not None:
        task_in_account(session, owner.user_id, parent_id)
    if not allow_duplicate:
        _check_duplicate(session, owner.user_id, clean_title, stored_due)
    moment = now()
    task = Task(
        parent_id=parent_id,
        title=clean_title,
        notes=notes,
        tags=stored_tags(tags),
        status=_check_status(status),
        estimate_minutes=_check_estimate(estimate_minutes),
        due_at=stored_due,
        created_by=actor_id(actor),
        created_at=moment,
        updated_by=actor_id(actor),
        updated_at=moment,
        owned_by=owner.user_id,
    )
    session.add(task)
    session.flush()
    return _detail(session, task)


def get_task(session: Session, actor: User, task_id: int) -> TaskDetail:
    owner = account_of(session, actor)
    return _detail(session, task_in_account(session, owner.user_id, task_id))


def list_tasks(
    session: Session,
    actor: User,
    parent_id: int | None = None,
    *,
    tags: str | None = None,
    open_only: bool = False,
) -> list[TaskSummary]:
    """One level: the roots, or the direct children of a parent."""
    owner = account_of(session, actor)
    tree = load_tree(session, owner.user_id)
    _check_parent_in(tree, parent_id)
    rows = tree.children(parent_id)
    return _summaries(
        session, owner.user_id, tree, rows, tags=tags, open_only=open_only, ancestors=False
    )


def task_tree(
    session: Session,
    actor: User,
    parent_id: int | None = None,
    *,
    tags: str | None = None,
    open_only: bool = False,
) -> list[TaskSummary]:
    """Every task under a parent, or in the account for None, parents before children.

    A filtered tree keeps each match's ancestors under the parent, marked unmatched, so
    no task comes back without the tasks above it.
    """
    owner = account_of(session, actor)
    tree = load_tree(session, owner.user_id)
    _check_parent_in(tree, parent_id)
    rows = tree.descendants(parent_id)
    return _summaries(
        session, owner.user_id, tree, rows, tags=tags, open_only=open_only, ancestors=True
    )


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
    append_notes: str | None = None,
    add: str | None = None,
    remove: str | None = None,
    release_blocks: bool = False,
    expected_updated_at: datetime | None = None,
) -> TaskDetail:
    """Change only the fields sent. A change that leaves the task as it was writes nothing.

    append_notes, add, and remove apply to the stored value inside this write.
    expected_updated_at refuses the update as a conflict when the task changed since.
    """
    owner = account_of(session, actor)
    task = task_in_account(session, owner.user_id, task_id)
    _check_current(task, expected_updated_at, owner.timezone)
    if notes is not UNSET and append_notes is not None:
        raise Invalid("send notes or append_notes, not both")
    if tags is not UNSET and (add is not None or remove is not None):
        raise Invalid("send tags or add and remove, not both")

    changes: dict[str, Any] = {}
    if title is not UNSET:
        changes["title"] = normalize_title(title)
    if notes is not UNSET:
        changes["notes"] = notes
    if append_notes is not None and append_notes.strip():
        changes["notes"] = append_note(task.notes, append_notes)
    if tags is not UNSET:
        changes["tags"] = stored_tags(tags)
    if add is not None or remove is not None:
        changes["tags"] = remove_tags(add_tags(task.tags, add or ""), remove or "")
    if status is not UNSET:
        changes["status"] = _check_status(status)
    if estimate_minutes is not UNSET:
        changes["estimate_minutes"] = _check_estimate(estimate_minutes)
    if due_at is not UNSET:
        changes["due_at"] = to_utc(due_at, owner.timezone) if due_at is not None else None
    if parent_id is not UNSET:
        _check_move(session, owner.user_id, task, parent_id)
        changes["parent_id"] = parent_id
    return _apply(session, actor, task, changes, release_blocks=release_blocks)


def drop_task(
    session: Session,
    actor: User,
    task_id: int,
    reason: str | None = None,
    *,
    release_blocks: bool = False,
    expected_updated_at: datetime | None = None,
) -> TaskDetail:
    """Set the task to dropped. A reason is appended to notes as a dropped block."""
    owner = account_of(session, actor)
    task = task_in_account(session, owner.user_id, task_id)
    _check_current(task, expected_updated_at, owner.timezone)
    changes: dict[str, Any] = {"status": DROPPED}
    if reason is not None and reason.strip():
        changes["notes"] = append_note(close_open_fence(task.notes), dropped_block(reason))
    return _apply(session, actor, task, changes, release_blocks=release_blocks)


def delete_task(session: Session, actor: User, task_id: int) -> None:
    """Delete the task. The cascade takes its subtree, blocks, and history."""
    owner = account_of(session, actor)
    task = task_in_account(session, owner.user_id, task_id)
    gone = {task_id} | {row.id for row in load_tree(session, owner.user_id).descendants(task_id)}
    session.delete(task)
    session.flush()
    # The database cascade removed these rows, so the session must stop handing them out.
    for obj in list(session.identity_map.values()):
        on_task = obj.id if isinstance(obj, Task) else getattr(obj, "task_id", None)
        if isinstance(obj, Task | Block | TaskHistory) and on_task in gone:
            session.expunge(obj)


def task_history(session: Session, actor: User, task_id: int) -> list[HistoryEntry]:
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
    """Mark the task changed by the actor. A block change calls this, and it writes no snapshot."""
    task.updated_at = now()
    task.updated_by = actor_id(actor)


def load_tree(session: Session, account_id: int) -> Tree:
    """The account's tree, from one query of the columns it needs."""
    names = (
        "id",
        "parent_id",
        "title",
        "status",
        "tags",
        "estimate_minutes",
        "due_at",
        "created_by",
        "created_at",
        "updated_by",
        "updated_at",
    )
    columns = [col(getattr(Task, name)) for name in names]
    # sqlmodel's select types at most four columns, so the tree load uses sqlalchemy's.
    rows = session.execute(sql_select(*columns).where(col(Task.owned_by) == account_id)).all()
    return Tree(
        TaskRow(
            id=row[0],
            parent_id=row[1],
            title=row[2],
            status=row[3],
            tags=row[4] or "",
            estimate_minutes=row[5],
            due_at=as_utc(row[6]) if row[6] is not None else None,
            created_by=row[7],
            created_at=as_utc(row[8]),
            updated_by=row[9],
            updated_at=as_utc(row[10]),
        )
        for row in rows
    )


def normalize_title(title: str) -> str:
    """The stored form of a title: trimmed, each run of whitespace one space."""
    clean = " ".join((title or "").split())
    if not clean:
        raise Invalid("title is required")
    return clean


def find_duplicate(
    session: Session, account_id: int, title: str, due_at: datetime | None
) -> Task | None:
    """A task in the account that a create with this title and due_at would duplicate."""
    query = select(Task).where(Task.owned_by == account_id, Task.title_key == title_key(title))
    if due_at is None:
        query = query.where(col(Task.due_at).is_(None), col(Task.status).in_(OPEN))
    else:
        query = query.where(Task.due_at == due_at)
    return session.exec(query.order_by(col(Task.id)).limit(1)).first()


def open_block_task_ids(session: Session, account_id: int) -> set[int]:
    query = (
        select(Block.task_id)
        .join(Task, col(Task.id) == Block.task_id)
        .where(Task.owned_by == account_id, col(Block.released_at).is_(None))
    )
    return set(session.exec(query).all())


def _apply(
    session: Session,
    actor: User,
    task: Task,
    changes: dict[str, Any],
    *,
    release_blocks: bool,
) -> TaskDetail:
    changed = {
        name: value for name, value in changes.items() if not _same(getattr(task, name), value)
    }
    if not changed:
        return _detail(session, task)
    if "status" in changed:
        _release_or_refuse(session, actor, task, changed["status"], release_blocks)
    for name, value in changed.items():
        setattr(task, name, value)
    touch(task, actor)
    session.add(task)
    session.flush()
    return _detail(session, task)


def _same(stored: Any, sent: Any) -> bool:
    """A null text column reads as "", so it equals an empty string."""
    if isinstance(stored, str | None) and isinstance(sent, str | None):
        return (stored or "") == (sent or "")
    if isinstance(stored, datetime) and isinstance(sent, datetime):
        return as_utc(stored) == as_utc(sent)
    return bool(stored == sent)


def _release_or_refuse(
    session: Session, actor: User, task: Task, status: int, release_blocks: bool
) -> None:
    """A blocked task keeps its status, unless a close asks to release the blocks first."""
    blocks = session.exec(
        select(Block).where(Block.task_id == task.id, col(Block.released_at).is_(None))
    ).all()
    if not blocks:
        return
    if not (release_blocks and status in CLOSED):
        raise Conflict("task is blocked")
    moment = now()
    for block in blocks:
        block.released_by = actor_id(actor)
        block.released_at = moment
        session.add(block)
    # The releases go first, since the schema refuses a status change on a blocked task.
    session.flush()


def _check_duplicate(
    session: Session, account_id: int, title: str, due_at: datetime | None
) -> None:
    match = find_duplicate(session, account_id, title, due_at)
    if match is not None:
        assert match.id is not None
        due = as_utc(match.due_at) if match.due_at is not None else None
        raise Duplicate(match.id, match.title, match.status, due)


def _check_move(session: Session, account_id: int, task: Task, parent_id: int | None) -> None:
    if parent_id is None:
        return
    tree = load_tree(session, account_id)
    if parent_id not in tree:
        raise NotFound("no such parent")
    assert task.id is not None
    if tree.would_cycle(task.id, parent_id):
        raise Invalid("a task cannot be nested under itself or a task under it")


def _check_parent_in(tree: Tree, parent_id: int | None) -> None:
    if parent_id is not None and parent_id not in tree:
        raise NotFound("no such task")


def _check_current(task: Task, expected_updated_at: datetime | None, timezone: str) -> None:
    """Refuse the write when the task changed since the caller read it.

    An updated_at sent without an offset is read in the account's timezone, like any
    other datetime a caller sends.
    """
    if expected_updated_at is None:
        return
    if as_utc(task.updated_at) != to_utc(expected_updated_at, timezone):
        raise Conflict("task changed since it was read")


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


def task_in_account(session: Session, account_id: int, task_id: int) -> Task:
    """A task in the account. A task in another account is not found."""
    task = session.exec(
        select(Task).where(Task.id == task_id, Task.owned_by == account_id)
    ).one_or_none()
    if task is None:
        raise NotFound("no such task")
    return task


def _detail(session: Session, task: Task) -> TaskDetail:
    assert task.id is not None
    tree = load_tree(session, task.owned_by)
    blocked = task.id in open_block_task_ids(session, task.owned_by)
    return TaskDetail(
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
        effectively_dropped=tree.is_effectively_dropped(task.id),
        blocked=blocked,
        dropped_reason=dropped_reason(task.notes) if task.status == DROPPED else "",
    )


def _summaries(
    session: Session,
    account_id: int,
    tree: Tree,
    rows: list[TaskRow],
    *,
    tags: str | None,
    open_only: bool,
    ancestors: bool,
) -> list[TaskSummary]:
    def matches(row: TaskRow) -> bool:
        if open_only and (row.status not in OPEN or tree.is_effectively_dropped(row.id)):
            return False
        return not tags or has_tags(row.tags, tags)

    matched = {row.id for row in rows if matches(row)}
    included = set(matched)
    if ancestors:
        in_rows = {row.id for row in rows}
        for task_id in matched:
            included.update(node for node in tree.ancestors(task_id) if node in in_rows)
    blocked = open_block_task_ids(session, account_id)
    summaries = []
    for row in rows:
        if row.id not in included:
            continue
        summaries.append(
            TaskSummary(
                id=row.id,
                parent_id=row.parent_id,
                title=row.title,
                tags=read_tags(row.tags),
                status=row.status,
                estimate_minutes=row.estimate_minutes,
                due_at=row.due_at,
                created_by=row.created_by,
                created_at=row.created_at,
                updated_by=row.updated_by,
                updated_at=row.updated_at,
                effectively_dropped=tree.is_effectively_dropped(row.id),
                blocked=row.id in blocked,
                matched=row.id in matched,
            )
        )
    return summaries
