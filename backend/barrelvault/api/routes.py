"""Agents, tasks, and blocks. Each route calls the service with the acting user."""

from typing import Any

from fastapi import APIRouter, Response, status
from fastapi.responses import JSONResponse
from sqlmodel import Session

from barrelvault.account import (
    UserRef,
    add_agent,
    agent_by_name,
    delete_agent,
    issue_key,
    list_agents,
    reassign_agent,
    retire_agent,
    roll_key,
    user_refs,
)
from barrelvault.api.deps import Actor, AppSettings, DbSession, Warnings
from barrelvault.api.schemas import (
    AgentIn,
    AgentOut,
    BlockIn,
    BlockOut,
    DropIn,
    ErrorOut,
    HistoryOut,
    KeyOut,
    TaskCreate,
    TaskEntryOut,
    TaskOut,
    TaskPatch,
    UserOut,
)
from barrelvault.blocks import BlockView, add_block, edit_block_reason, list_blocks, release_block
from barrelvault.models import User
from barrelvault.tasks import (
    UNSET,
    TaskDetail,
    TaskSummary,
    delete_task,
    drop_task,
    get_task,
    list_tasks,
    task_history,
    task_tree,
    update_task,
)

router = APIRouter()

_ATTACHED: dict[int | str, dict[str, Any]] = {status.HTTP_409_CONFLICT: {"model": ErrorOut}}


# Agents. Only the person calls these, and the service refuses an agent.


def _agent_out(session: Session, actor: User, name: str) -> AgentOut:
    """The agent the route just added or changed, as the person's agent list shows it."""
    return next(AgentOut(**vars(a)) for a in list_agents(session, actor) if a.name == name)


@router.get("/agents")
def agents_route(session: DbSession, actor: Actor) -> list[AgentOut]:
    return [AgentOut(**vars(agent)) for agent in list_agents(session, actor)]


@router.post("/agents", status_code=status.HTTP_201_CREATED)
def add_agent_route(body: AgentIn, session: DbSession, actor: Actor) -> AgentOut:
    add_agent(session, actor, body.name)
    return _agent_out(session, actor, body.name)


@router.post("/agents/{name}/key", status_code=status.HTTP_201_CREATED)
def issue_key_route(name: str, session: DbSession, actor: Actor, settings: AppSettings) -> KeyOut:
    agent_id = agent_by_name(session, actor, name)
    issued = issue_key(session, actor, agent_id, key_length=settings.key_length)
    return KeyOut(key=issued.key, key_expires_at=issued.key_expires_at)


@router.post("/agents/{name}/key/roll", status_code=status.HTTP_201_CREATED)
def roll_key_route(name: str, session: DbSession, actor: Actor, settings: AppSettings) -> KeyOut:
    agent_id = agent_by_name(session, actor, name)
    issued = roll_key(session, actor, agent_id, key_length=settings.key_length)
    return KeyOut(key=issued.key, key_expires_at=issued.key_expires_at)


@router.post("/agents/{name}/retire")
def retire_route(name: str, session: DbSession, actor: Actor) -> AgentOut:
    retire_agent(session, actor, agent_by_name(session, actor, name))
    return _agent_out(session, actor, name)


@router.post("/agents/{name}/reassign", status_code=status.HTTP_204_NO_CONTENT)
def reassign_route(name: str, session: DbSession, actor: Actor) -> None:
    reassign_agent(session, actor, agent_by_name(session, actor, name))


@router.delete(
    "/agents/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    responses=_ATTACHED,
)
def delete_agent_route(name: str, session: DbSession, actor: Actor) -> Response:
    """Delete an agent with nothing attached, or answer attached so the person can choose."""
    if delete_agent(session, actor, agent_by_name(session, actor, name)):
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    message = "the agent has attached work; reassign it or retire the agent"
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"error": "attached", "message": message},
    )


# Tasks.


def _named(session: Session, user_ids: set[int]) -> dict[int, UserOut]:
    refs: dict[int, UserRef] = user_refs(session, user_ids)
    return {user_id: UserOut(name=ref.name, kind=ref.kind) for user_id, ref in refs.items()}


def _task_out(session: Session, task: TaskDetail) -> TaskOut:
    names = _named(session, {task.created_by, task.updated_by})
    fields = {k: v for k, v in vars(task).items() if k != "owned_by"}
    fields["created_by"], fields["updated_by"] = names[task.created_by], names[task.updated_by]
    return TaskOut(**fields)


def _entries_out(session: Session, tasks: list[TaskSummary]) -> list[TaskEntryOut]:
    names = _named(session, {t.created_by for t in tasks} | {t.updated_by for t in tasks})
    out = []
    for task in tasks:
        fields = dict(vars(task))
        fields["created_by"], fields["updated_by"] = names[task.created_by], names[task.updated_by]
        out.append(TaskEntryOut(**fields))
    return out


@router.get("/tasks")
def list_tasks_route(
    session: DbSession,
    actor: Actor,
    parent: int | None = None,
    tags: str | None = None,
    open: bool = False,
) -> list[TaskEntryOut]:
    tasks = list_tasks(session, actor, parent, tags=tags, open_only=open)
    return _entries_out(session, tasks)


@router.get("/tasks/tree")
def task_tree_route(
    session: DbSession,
    actor: Actor,
    parent: int | None = None,
    tags: str | None = None,
    open: bool = False,
) -> list[TaskEntryOut]:
    return _entries_out(session, task_tree(session, actor, parent, tags=tags, open_only=open))


@router.get("/tasks/{task_id}")
def get_task_route(task_id: int, session: DbSession, actor: Actor) -> TaskOut:
    return _task_out(session, get_task(session, actor, task_id))


@router.post("/tasks", status_code=status.HTTP_201_CREATED)
def create_task_route(
    body: TaskCreate, session: DbSession, actor: Actor, warnings: Warnings
) -> TaskOut:
    fields = body.model_dump(exclude={"title", "allow_duplicate"})
    task = warnings.create(
        session, actor, body.title, allow_duplicate=body.allow_duplicate, **fields
    )
    return _task_out(session, task)


@router.patch("/tasks/{task_id}")
def update_task_route(task_id: int, body: TaskPatch, session: DbSession, actor: Actor) -> TaskOut:
    sent = body.model_fields_set
    fields: dict[str, Any] = {
        name: getattr(body, name) if name in sent else UNSET
        for name in ("title", "notes", "tags", "status", "estimate_minutes", "due_at", "parent_id")
    }
    task = update_task(
        session,
        actor,
        task_id,
        **fields,
        append_notes=body.append_notes,
        add=body.add_tags,
        remove=body.remove_tags,
        release_blocks=body.release_blocks,
        expected_updated_at=body.expected_updated_at,
    )
    return _task_out(session, task)


@router.post("/tasks/{task_id}/drop")
def drop_task_route(task_id: int, body: DropIn, session: DbSession, actor: Actor) -> TaskOut:
    task = drop_task(
        session,
        actor,
        task_id,
        body.reason,
        release_blocks=body.release_blocks,
        expected_updated_at=body.expected_updated_at,
    )
    return _task_out(session, task)


@router.delete("/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_task_route(task_id: int, session: DbSession, actor: Actor) -> None:
    delete_task(session, actor, task_id)


@router.get("/tasks/{task_id}/history")
def history_route(task_id: int, session: DbSession, actor: Actor) -> list[HistoryOut]:
    entries = task_history(session, actor, task_id)
    names = _named(session, {entry.user_id for entry in entries})
    return [
        HistoryOut(editor=names[entry.user_id], created_at=entry.created_at, state=entry.state)
        for entry in entries
    ]


# Blocks.


def _block_out(session: Session, block: BlockView) -> BlockOut:
    ids = {block.created_by} | ({block.released_by} if block.released_by else set())
    names = _named(session, ids)
    fields = dict(vars(block))
    fields["created_by"] = names[block.created_by]
    fields["released_by"] = names[block.released_by] if block.released_by else None
    return BlockOut(**fields)


@router.get("/tasks/{task_id}/blocks")
def list_blocks_route(task_id: int, session: DbSession, actor: Actor) -> list[BlockOut]:
    return [_block_out(session, block) for block in list_blocks(session, actor, task_id)]


@router.post("/tasks/{task_id}/blocks", status_code=status.HTTP_201_CREATED)
def add_block_route(task_id: int, body: BlockIn, session: DbSession, actor: Actor) -> BlockOut:
    return _block_out(session, add_block(session, actor, task_id, body.reason))


@router.patch("/blocks/{block_id}")
def edit_block_route(block_id: int, body: BlockIn, session: DbSession, actor: Actor) -> BlockOut:
    return _block_out(session, edit_block_reason(session, actor, block_id, body.reason))


@router.post("/blocks/{block_id}/release")
def release_block_route(block_id: int, session: DbSession, actor: Actor) -> BlockOut:
    return _block_out(session, release_block(session, actor, block_id))
