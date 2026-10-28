"""Block reads and writes. Each call takes the acting user and reads no session.

A block is the record of what a task waits on. Every change to one marks its task
changed by the actor, which moves the task's updated_at without a history snapshot.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlmodel import Session, col, select

from barrelvault.account import account_of, actor_id
from barrelvault.errors import Conflict, Invalid, NotFound
from barrelvault.models import Block, Task, User
from barrelvault.status import OPEN
from barrelvault.tasks import load_tree, task_in_account, touch
from barrelvault.times import as_utc, now


@dataclass(frozen=True)
class FollowUp:
    block: "BlockView"
    task_title: str


@dataclass(frozen=True)
class BlockView:
    id: int
    task_id: int
    reason: str
    created_by: int
    created_at: datetime
    released_by: int | None
    released_at: datetime | None


def add_block(session: Session, actor: User, task_id: int, reason: str) -> BlockView:
    """Add an open block to an open task."""
    owner = account_of(session, actor)
    task = task_in_account(session, owner.user_id, task_id)
    if task.status not in OPEN:
        raise Conflict("a block is added only to an open task")
    block = Block(
        task_id=task_id,
        reason=_check_reason(reason),
        created_by=actor_id(actor),
        created_at=now(),
    )
    session.add(block)
    touch(task, actor)
    session.add(task)
    session.flush()
    return _view(block)


def release_block(session: Session, actor: User, block_id: int) -> BlockView:
    """Release an open block as the actor. A released block stays released."""
    block, task = _block(session, actor, block_id)
    if block.released_at is not None:
        raise Conflict("block is already released")
    block.released_by = actor_id(actor)
    block.released_at = now()
    return _save(session, actor, block, task)


def edit_block_reason(session: Session, actor: User, block_id: int, reason: str) -> BlockView:
    block, task = _block(session, actor, block_id)
    clean = _check_reason(reason)
    if clean == block.reason:
        return _view(block)
    block.reason = clean
    return _save(session, actor, block, task)


def list_blocks(session: Session, actor: User, task_id: int) -> list[BlockView]:
    """A task's blocks, open and released, in the order they were added."""
    owner = account_of(session, actor)
    task_in_account(session, owner.user_id, task_id)
    query = (
        select(Block).where(Block.task_id == task_id).order_by(col(Block.created_at), col(Block.id))
    )
    return [_view(block) for block in session.exec(query)]


def blocks_to_follow_up(session: Session, actor: User) -> list[FollowUp]:
    """Every open block in the account, leaving out effectively dropped tasks."""
    owner = account_of(session, actor)
    tree = load_tree(session, owner.user_id)
    query = (
        select(Block, Task.title)
        .join(Task, col(Task.id) == Block.task_id)
        .where(Task.owned_by == owner.user_id, col(Block.released_at).is_(None))
        .order_by(col(Block.created_at), col(Block.id))
    )
    return [
        FollowUp(_view(block), title)
        for block, title in session.exec(query)
        if not tree.is_effectively_dropped(block.task_id)
    ]


def _block(session: Session, actor: User, block_id: int) -> tuple[Block, Task]:
    """A block on a task in the actor's account. Any other block is not found."""
    owner = account_of(session, actor)
    row = session.exec(
        select(Block, Task)
        .join(Task, col(Task.id) == Block.task_id)
        .where(Block.id == block_id, Task.owned_by == owner.user_id)
    ).one_or_none()
    if row is None:
        raise NotFound("no such block")
    return row


def _save(session: Session, actor: User, block: Block, task: Task) -> BlockView:
    session.add(block)
    touch(task, actor)
    session.add(task)
    session.flush()
    return _view(block)


def _check_reason(reason: str) -> str:
    clean = (reason or "").strip()
    if not clean:
        raise Invalid("reason is required")
    if "\n" in clean or "\r" in clean:
        raise Invalid("reason is one line")
    return clean


def _view(block: Block) -> BlockView:
    assert block.id is not None
    return BlockView(
        id=block.id,
        task_id=block.task_id,
        reason=block.reason,
        created_by=block.created_by,
        created_at=as_utc(block.created_at),
        released_by=block.released_by,
        released_at=as_utc(block.released_at) if block.released_at is not None else None,
    )
