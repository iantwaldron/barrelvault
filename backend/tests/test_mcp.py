"""The MCP server, through the SDK's client against the app, in process."""

import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from sqlmodel import SQLModel

from barrelvault import db
from barrelvault.account import create_account
from barrelvault.api.app import create_app
from barrelvault.api.deps import COOKIE
from barrelvault.mcp_server import WRITTEN_TEXT
from barrelvault.settings import load_settings
from tests.support import second_account, set_token_expiry

pytestmark = pytest.mark.anyio

BASE = "https://testserver"
UTC_TEXT = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
REFUSED = {"error": "refused", "message": "refused"}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def app(database):
    SQLModel.metadata.create_all(db.get_engine())
    with db.open_session() as session:
        create_account(session, "Ada", "ada@example.com", "password-1", "America/Chicago")
    return create_app(load_settings())


def _web(app) -> TestClient:
    return TestClient(app, base_url=BASE)


@pytest.fixture
def ada(app) -> TestClient:
    client = _web(app)
    client.post("/api/login", json={"email": "ada@example.com", "password": "password-1"})
    return client


def _agent_token(app, person: TestClient, name: str) -> str:
    person.post("/api/agents", json={"name": name})
    key = person.post(f"/api/agents/{name}/key", json={}).json()["key"]
    return _web(app).post("/api/exchange", json={"key": key}).json()["token"]


@pytest.fixture
def token(app, ada) -> str:
    return _agent_token(app, ada, "Bot")


@pytest.fixture
async def serving(app) -> AsyncIterator[None]:
    """The app's lifespan runs the MCP session manager, which starts once per app."""
    async with app.router.lifespan_context(app):
        yield


@asynccontextmanager
async def _mcp(app, headers: dict[str, str]) -> AsyncIterator[Client]:
    transport = httpx2.ASGITransport(app=app)
    async with (
        httpx2.AsyncClient(transport=transport, base_url=BASE, headers=headers) as http,
        Client(streamable_http_client(f"{BASE}/mcp", http_client=http)) as client,
    ):
        yield client


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _call(client: Client, tool: str, **arguments: Any) -> Any:
    """A tool's structured result, unwrapping the "result" a list tool is published under."""
    result = await client.call_tool(tool, arguments)
    assert not result.is_error, result.content
    data = result.structured_content
    return data["result"] if isinstance(data, dict) and set(data) == {"result"} else data


async def _refusal(client: Client, tool: str, **arguments: Any) -> dict[str, Any]:
    result = await client.call_tool(tool, arguments)
    assert result.is_error
    return json.loads(result.content[0].text)


def _ids_absent(value: Any) -> None:
    if isinstance(value, list):
        for item in value:
            _ids_absent(item)
    elif isinstance(value, dict):
        assert not {"user_id", "owned_by", "owner_id"} & set(value)
        for key, item in value.items():
            if key.endswith("_by") or key == "editor":
                assert item is None or set(item) == {"name", "kind"}
            else:
                _ids_absent(item)


async def test_every_tool_works_for_an_agent(serving, app, token):
    async with _mcp(app, _bearer(token)) as client:
        names = sorted(tool.name for tool in (await client.list_tools()).tools)
        assert names == [
            "add_block",
            "blocks_to_follow_up",
            "create_task",
            "delete_task",
            "drop_task",
            "edit_block",
            "get_account",
            "get_task",
            "get_tree",
            "list_tasks",
            "release_block",
            "task_history",
            "update_task",
        ]
        account = await _call(client, "get_account")
        assert account == {"name": "Bot", "kind": "agent", "timezone": "America/Chicago"}
        project = await _call(client, "create_task", title="Project", due_at="2026-07-01T17:00:00")
        assert project["due_at"] == "2026-07-01T22:00:00.000Z"
        assert project["created_by"] == {"name": "Bot", "kind": "agent"}
        step = await _call(client, "create_task", title="Step", parent_id=project["id"])
        assert [t["id"] for t in await _call(client, "list_tasks")] == [project["id"]]
        tree = await _call(client, "get_tree", parent_id=project["id"])
        assert [t["id"] for t in tree] == [step["id"]]
        updated = await _call(
            client,
            "update_task",
            task_id=step["id"],
            append_notes="Asked Chad.",
            add_tags="Deal:Chad",
            clear=["parent_id"],
        )
        assert (updated["notes"], updated["tags"], updated["parent_id"]) == (
            "Asked Chad.",
            ["deal:chad"],
            None,
        )
        block = await _call(client, "add_block", task_id=step["id"], reason="Waiting on Chad")
        edited = await _call(client, "edit_block", block_id=block["id"], reason="Waiting on Sam")
        assert edited["reason"] == "Waiting on Sam"
        follow_ups = await _call(client, "blocks_to_follow_up")
        assert [(f["id"], f["task_title"]) for f in follow_ups] == [(block["id"], "Step")]
        read = await _call(client, "get_task", task_id=step["id"])
        assert [b["id"] for b in read["blocks"]] == [block["id"]] and read["blocked"]
        released = await _call(client, "release_block", block_id=block["id"])
        assert released["released_by"] == {"name": "Bot", "kind": "agent"}
        dropped = await _call(client, "drop_task", task_id=step["id"], reason="Superseded.")
        assert (dropped["status"], dropped["dropped_reason"]) == (3, "Superseded.")
        history = await _call(client, "task_history", task_id=step["id"])
        assert [entry["editor"]["name"] for entry in history] == ["Bot", "Bot", "Bot"]
        assert await _call(client, "delete_task", task_id=project["id"]) is None
        assert await _call(client, "list_tasks") == [
            {**entry, "parent_id": None}
            for entry in await _call(client, "list_tasks")
            if entry["id"] == step["id"]
        ]
        for result in (project, step, updated, read, follow_ups, history):
            _ids_absent(result)
        for moment in (project["created_at"], released["released_at"], history[0]["created_at"]):
            assert UTC_TEXT.match(moment)


async def test_a_stale_read_and_a_blocked_status_are_conflicts(serving, app, token):
    async with _mcp(app, _bearer(token)) as client:
        task = await _call(client, "create_task", title="Call Chad")
        await _call(client, "update_task", task_id=task["id"], title="Call Chad back")
        stale = await _refusal(
            client,
            "update_task",
            task_id=task["id"],
            status=2,
            expected_updated_at=task["updated_at"],
        )
        assert stale == {"error": "conflict", "message": "task changed since it was read"}
        await _call(client, "add_block", task_id=task["id"], reason="Waiting")
        held = await _refusal(client, "update_task", task_id=task["id"], status=2)
        assert held == {"error": "conflict", "message": "task is blocked"}
        closed = await _call(
            client, "update_task", task_id=task["id"], status=2, release_blocks=True
        )
        assert closed["status"] == 2


async def test_another_account_is_not_found_on_every_task_and_block_tool(serving, app, ada, token):
    with db.open_session() as session:
        _bea_id, bea_secret = second_account(session)
    bea = _web(app)
    bea.cookies.set(COOKIE, bea_secret)
    beas_token = _agent_token(app, bea, "Bea bot")
    async with _mcp(app, _bearer(token)) as mine:
        task = await _call(mine, "create_task", title="Mine")
        block = await _call(mine, "add_block", task_id=task["id"], reason="Waiting")
    calls = (
        ("get_task", {"task_id": task["id"]}),
        ("get_tree", {"parent_id": task["id"]}),
        ("list_tasks", {"parent_id": task["id"]}),
        ("update_task", {"task_id": task["id"], "title": "Taken"}),
        ("drop_task", {"task_id": task["id"]}),
        ("delete_task", {"task_id": task["id"]}),
        ("task_history", {"task_id": task["id"]}),
        ("create_task", {"title": "Nested", "parent_id": task["id"]}),
        ("add_block", {"task_id": task["id"], "reason": "Taken"}),
        ("edit_block", {"block_id": block["id"], "reason": "Taken"}),
        ("release_block", {"block_id": block["id"]}),
    )
    async with _mcp(app, _bearer(beas_token)) as theirs:
        for tool, arguments in calls:
            assert (await _refusal(theirs, tool, **arguments))["error"] == "not_found", tool
        assert await _call(theirs, "list_tasks") == []
        assert await _call(theirs, "blocks_to_follow_up") == []


async def test_a_call_without_a_bearer_session_is_refused(serving, app, ada, token):
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    accept = {"accept": "application/json, text/event-stream"}
    expired = _agent_token(app, ada, "Expired")
    with db.open_session() as session:
        user_id = (
            session.connection()
            .exec_driver_sql("SELECT id FROM user WHERE name = 'Expired'")
            .scalar_one()
        )
        set_token_expiry(session, user_id, "2000-01-01 00:00:00")
    cookie = ada.cookies.get(COOKIE)
    transport = httpx2.ASGITransport(app=app)
    for headers in (
        {},
        _bearer("not-a-session"),
        _bearer(expired),
        {"cookie": f"{COOKIE}={cookie}"},
    ):
        async with httpx2.AsyncClient(transport=transport, base_url=BASE) as http:
            response = await http.post("/mcp", json=request, headers={**accept, **headers})
        assert (response.status_code, response.json()) == (401, REFUSED)


async def test_each_tool_declares_its_annotations_and_marks_written_text(serving, app, token):
    async with _mcp(app, _bearer(token)) as client:
        assert WRITTEN_TEXT in (client.instructions or "")
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    read_only = {
        "get_account",
        "list_tasks",
        "get_tree",
        "get_task",
        "blocks_to_follow_up",
        "task_history",
    }
    for name, tool in tools.items():
        annotations = tool.annotations
        assert annotations is not None and annotations.open_world_hint is False, name
        assert annotations.read_only_hint is (name in read_only), name
        if name not in read_only:
            assert annotations.destructive_hint is (name == "delete_task"), name
            assert annotations.idempotent_hint is (name == "edit_block"), name
    # Every tool that returns written text marks it as data.
    for name in set(tools) - {"get_account", "delete_task"}:
        assert WRITTEN_TEXT in (tools[name].description or ""), name
    assert "drop_task" in (tools["delete_task"].description or "")
    assert "parent_id" in tools["get_tree"].input_schema["required"]


async def test_arguments_are_held_to_the_apis_rules(serving, app, token):
    async with _mcp(app, _bearer(token)) as client:
        task = await _call(client, "create_task", title="Call Chad", notes="Asked.")
        refusals = {
            "parent_id: Field required": ("get_tree", {}),
            "titel: Extra inputs are not permitted": (
                "update_task",
                {"task_id": task["id"], "titel": "Lost"},
            ),
            "status: Input should be a valid integer": (
                "update_task",
                {"task_id": task["id"], "status": "1"},
            ),
            "notes: sent and cleared": (
                "update_task",
                {"task_id": task["id"], "notes": "Kept", "clear": ["notes"]},
            ),
            "time is out of range": (
                "create_task",
                {"title": "Far", "due_at": "9999-12-31T23:00:00-05:00"},
            ),
        }
        for message, (tool, arguments) in refusals.items():
            refusal = await _refusal(client, tool, **arguments)
            assert refusal["error"] == "invalid", (tool, refusal)
            assert refusal["message"] == message, (tool, refusal)
        unknown_field = await _refusal(client, "update_task", task_id=task["id"], clear=["due"])
        assert unknown_field["error"] == "invalid" and unknown_field["message"].startswith(
            "clear.0"
        )
        assert (await _refusal(client, "update_task", task_id=task["id"], status=True))[
            "error"
        ] == "invalid"
        for tool, arguments in (
            ("get_task", {"task_id": 2**70}),
            ("release_block", {"block_id": 2**70}),
        ):
            assert (await _refusal(client, tool, **arguments))["error"] == "not_found", tool
        # A tool that does not exist is the SDK's error, not a refused argument.
        missing = await client.call_tool("no_such_tool", {"task_id": task["id"]})
        assert missing.is_error and "no_such_tool" in missing.content[0].text
        unchanged = await _call(client, "get_task", task_id=task["id"])
        assert (unchanged["title"], unchanged["notes"], unchanged["status"]) == (
            "Call Chad",
            "Asked.",
            0,
        )


async def test_a_duplicate_warned_through_the_api_is_confirmed_through_mcp(
    serving, app, ada, token
):
    ada.post("/api/tasks", json={"title": "Pay rent"})
    agent = _web(app)
    agent.headers["Authorization"] = f"Bearer {token}"
    warned = agent.post("/api/tasks", json={"title": "pay RENT"})
    assert warned.json()["error"] == "duplicate"
    async with _mcp(app, _bearer(token)) as client:
        early = await _refusal(client, "create_task", title="Water plants", allow_duplicate=True)
        assert early["error"] == "conflict"
        confirmed = await _call(client, "create_task", title="pay RENT", allow_duplicate=True)
        assert confirmed["title"] == "pay RENT"
        again = await _refusal(client, "create_task", title="Pay rent")
        assert again["error"] == "duplicate" and again["task"]["title"] == "Pay rent"
