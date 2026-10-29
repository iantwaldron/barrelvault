"""Agents, tasks, and blocks. Each route calls the service with the acting user."""

from typing import Any

from fastapi import APIRouter, Response, status
from fastapi.responses import JSONResponse
from sqlmodel import Session

from barrelvault.account import (
    add_agent,
    agent_by_name,
    delete_agent,
    issue_key,
    list_agents,
    reassign_agent,
    retire_agent,
    roll_key,
)
from barrelvault.api.deps import Actor, AppSettings, DbSession, Warnings
from barrelvault.api.schemas import (
    AgentIn,
    AgentOut,
    BlockIn,
    BlockOut,
    DropIn,
    HistoryOut,
    KeyOut,
    TaskCreate,
    TaskEntryOut,
    TaskOut,
    TaskPatch,
    errors,
)
from barrelvault.api.shapes import blocks_out, entries_out, history_out, task_out
from barrelvault.blocks import add_block, edit_block_reason, list_blocks, release_block
from barrelvault.models import User
from barrelvault.tasks import (
    EDITABLE_FIELDS,
    UNSET,
    delete_task,
    drop_task,
    get_task,
    list_tasks,
    task_history,
    task_tree,
    update_task,
)

# Every route here needs a session, names a row that may be missing, and checks its input.
router = APIRouter(responses=errors(401, 404, 422))
# Only the person manages agents, and a name, a key, or attached work can conflict.
_AGENT_ERRORS = errors(403, 409)
# A task or block write can meet a block, a stale read, or a released block.
_WRITE_ERRORS = errors(409)


# Agents. Only the person calls these, and the service refuses an agent.


def _agent_out(session: Session, actor: User, name: str) -> AgentOut:
    """The agent the route just added or changed, as the person's agent list shows it."""
    return next(AgentOut(**vars(a)) for a in list_agents(session, actor) if a.name == name)


@router.get("/agents", responses=errors(403))
def agents_route(session: DbSession, actor: Actor) -> list[AgentOut]:
    return [AgentOut(**vars(agent)) for agent in list_agents(session, actor)]


@router.post("/agents", status_code=status.HTTP_201_CREATED, responses=_AGENT_ERRORS)
def add_agent_route(body: AgentIn, session: DbSession, actor: Actor) -> AgentOut:
    add_agent(session, actor, body.name)
    return _agent_out(session, actor, body.name)


@router.post("/agents/{name}/key", status_code=status.HTTP_201_CREATED, responses=_AGENT_ERRORS)
def issue_key_route(name: str, session: DbSession, actor: Actor, settings: AppSettings) -> KeyOut:
    agent_id = agent_by_name(session, actor, name)
    issued = issue_key(session, actor, agent_id, key_length=settings.key_length)
    return KeyOut(key=issued.key, key_expires_at=issued.key_expires_at)


@router.post(
    "/agents/{name}/key/roll", status_code=status.HTTP_201_CREATED, responses=_AGENT_ERRORS
)
def roll_key_route(name: str, session: DbSession, actor: Actor, settings: AppSettings) -> KeyOut:
    agent_id = agent_by_name(session, actor, name)
    issued = roll_key(session, actor, agent_id, key_length=settings.key_length)
    return KeyOut(key=issued.key, key_expires_at=issued.key_expires_at)


@router.post("/agents/{name}/retire", responses=_AGENT_ERRORS)
def retire_route(name: str, session: DbSession, actor: Actor) -> AgentOut:
    retire_agent(session, actor, agent_by_name(session, actor, name))
    return _agent_out(session, actor, name)


@router.post(
    "/agents/{name}/reassign", status_code=status.HTTP_204_NO_CONTENT, responses=_AGENT_ERRORS
)
def reassign_route(name: str, session: DbSession, actor: Actor) -> None:
    reassign_agent(session, actor, agent_by_name(session, actor, name))


@router.delete(
    "/agents/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    responses=_AGENT_ERRORS,
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


@router.get("/tasks")
def list_tasks_route(
    session: DbSession,
    actor: Actor,
    parent: int | None = None,
    tags: str | None = None,
    open: bool = False,
) -> list[TaskEntryOut]:
    tasks = list_tasks(session, actor, parent, tags=tags, open_only=open)
    return entries_out(session, tasks)


@router.get("/tasks/tree")
def task_tree_route(
    session: DbSession,
    actor: Actor,
    parent: int | None = None,
    tags: str | None = None,
    open: bool = False,
) -> list[TaskEntryOut]:
    return entries_out(session, task_tree(session, actor, parent, tags=tags, open_only=open))


@router.get("/tasks/{task_id}")
def get_task_route(task_id: int, session: DbSession, actor: Actor) -> TaskOut:
    return task_out(session, get_task(session, actor, task_id))


@router.post("/tasks", status_code=status.HTTP_201_CREATED, responses=errors(duplicate=True))
def create_task_route(
    body: TaskCreate, session: DbSession, actor: Actor, warnings: Warnings
) -> TaskOut:
    fields = body.model_dump(exclude={"title", "allow_duplicate"})
    task = warnings.create(
        session, actor, body.title, allow_duplicate=body.allow_duplicate, **fields
    )
    return task_out(session, task)


@router.patch("/tasks/{task_id}", responses=_WRITE_ERRORS)
def update_task_route(task_id: int, body: TaskPatch, session: DbSession, actor: Actor) -> TaskOut:
    sent = body.model_fields_set
    fields: dict[str, Any] = {
        name: getattr(body, name) if name in sent else UNSET for name in EDITABLE_FIELDS
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
    return task_out(session, task)


@router.post("/tasks/{task_id}/drop", responses=_WRITE_ERRORS)
def drop_task_route(task_id: int, body: DropIn, session: DbSession, actor: Actor) -> TaskOut:
    task = drop_task(
        session,
        actor,
        task_id,
        body.reason,
        release_blocks=body.release_blocks,
        expected_updated_at=body.expected_updated_at,
    )
    return task_out(session, task)


@router.delete("/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_task_route(task_id: int, session: DbSession, actor: Actor) -> None:
    delete_task(session, actor, task_id)


@router.get("/tasks/{task_id}/history")
def history_route(task_id: int, session: DbSession, actor: Actor) -> list[HistoryOut]:
    return history_out(session, task_history(session, actor, task_id))


# Blocks.


@router.get("/tasks/{task_id}/blocks")
def list_blocks_route(task_id: int, session: DbSession, actor: Actor) -> list[BlockOut]:
    return blocks_out(session, list_blocks(session, actor, task_id))


@router.post(
    "/tasks/{task_id}/blocks", status_code=status.HTTP_201_CREATED, responses=_WRITE_ERRORS
)
def add_block_route(task_id: int, body: BlockIn, session: DbSession, actor: Actor) -> BlockOut:
    return blocks_out(session, [add_block(session, actor, task_id, body.reason)])[0]


@router.patch("/blocks/{block_id}", responses=_WRITE_ERRORS)
def edit_block_route(block_id: int, body: BlockIn, session: DbSession, actor: Actor) -> BlockOut:
    return blocks_out(session, [edit_block_reason(session, actor, block_id, body.reason)])[0]


@router.post("/blocks/{block_id}/release", responses=_WRITE_ERRORS)
def release_block_route(block_id: int, session: DbSession, actor: Actor) -> BlockOut:
    return blocks_out(session, [release_block(session, actor, block_id)])[0]
