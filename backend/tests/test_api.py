"""The REST API, through FastAPI's test client against the production engine setup."""

import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel

from barrelvault import auth, db
from barrelvault.account import create_account
from barrelvault.api.app import create_app
from barrelvault.api.deps import COOKIE
from barrelvault.settings import load_settings
from tests.support import second_account, set_token_expiry

UTC_TEXT = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
NO_SESSION = {"error": "refused", "message": "refused"}


@pytest.fixture
def app(database):
    SQLModel.metadata.create_all(db.get_engine())
    with db.open_session() as session:
        create_account(session, "Ada", "ada@example.com", "password-1", "America/Chicago")
    return create_app(load_settings())


def _client(app, scheme: str = "https") -> TestClient:
    return TestClient(app, base_url=f"{scheme}://testserver")


@pytest.fixture
def ada(app) -> TestClient:
    client = _client(app)
    response = client.post(
        "/api/login", json={"email": "ada@example.com", "password": "password-1"}
    )
    assert response.status_code == 204
    return client


def _agent(app, ada: TestClient, name: str = "Bot") -> TestClient:
    assert ada.post("/api/agents", json={"name": name}).status_code == 201
    key = ada.post(f"/api/agents/{name}/key", json={}).json()["key"]
    exchanged = _client(app).post("/api/exchange", json={"key": key})
    assert exchanged.status_code == 200
    client = _client(app)
    client.headers["Authorization"] = f"Bearer {exchanged.json()['token']}"
    return client


@pytest.fixture
def bot(app, ada) -> TestClient:
    return _agent(app, ada)


@pytest.fixture
def bea(app) -> TestClient:
    with db.open_session() as session:
        _bea_id, secret = second_account(session)
    client = _client(app)
    client.cookies.set(COOKIE, secret)
    return client


def _ids_absent(value: Any) -> None:
    """No response names a user by id: *_by fields are names, and no id field leaks."""
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


def test_login_sets_the_session_cookie(app):
    for scheme, secure in (("https", True), ("http", False)):
        client = _client(app, scheme)
        response = client.post(
            "/api/login", json={"email": "Ada@Example.com", "password": "password-1"}
        )
        assert response.status_code == 204
        cookie = response.headers["set-cookie"]
        assert f"{COOKIE}=" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
        assert ("Secure" in cookie) is secure


def test_a_failed_login_or_missing_session_is_refused_the_same_way(app, ada):
    client = _client(app)
    for body in (
        {"email": "ada@example.com", "password": "wrong"},
        {"email": "missing@example.com", "password": "password-1"},
    ):
        response = client.post("/api/login", json=body)
        assert (response.status_code, response.json()) == (401, NO_SESSION)
    for headers in ({}, {"Authorization": "Bearer not-a-session"}):
        response = client.get("/api/tasks", headers=headers)
        assert (response.status_code, response.json()) == (401, NO_SESSION)


def test_logout_ends_the_session_and_clears_the_cookie(app, ada):
    secret = ada.cookies.get(COOKIE)
    assert secret
    response = ada.post("/api/logout", json={})
    assert response.status_code == 204
    assert f'{COOKIE}=""' in response.headers["set-cookie"]
    stale = _client(app)
    stale.cookies.set(COOKIE, secret)
    assert stale.get("/api/account").status_code == 401


def test_an_expired_session_is_refused(app, ada):
    with db.open_session() as session:
        set_token_expiry(session, 1, "2000-01-01 00:00:00")
    assert (ada.get("/api/account").status_code, ada.get("/api/account").json()) == (
        401,
        NO_SESSION,
    )


def test_a_cookie_write_must_send_json(ada):
    response = ada.post(
        "/api/tasks", content="title=Forged", headers={"content-type": "text/plain"}
    )
    assert response.status_code == 422
    assert response.json() == {
        "error": "invalid",
        "message": "a write with the session cookie must send application/json",
    }
    assert ada.get("/api/tasks").json() == []


def test_the_exchange_gives_an_agent_a_bearer_session(app, ada):
    ada.post("/api/agents", json={"name": "Bot"})
    key = ada.post("/api/agents/Bot/key", json={}).json()["key"]
    exchanged = _client(app).post("/api/exchange", json={"key": key})
    assert UTC_TEXT.match(exchanged.json()["expires_at"])
    # The key itself is not a session.
    keyed = _client(app)
    keyed.headers["Authorization"] = f"Bearer {key}"
    assert keyed.get("/api/account").status_code == 401
    refused = _client(app).post("/api/exchange", json={"key": "nope"})
    assert (refused.status_code, refused.json()) == (401, NO_SESSION)


def test_the_account_names_the_actor_and_only_the_person_changes_the_timezone(ada, bot):
    assert ada.get("/api/account").json() == {
        "name": "Ada",
        "kind": "person",
        "email": "ada@example.com",
        "timezone": "America/Chicago",
    }
    assert bot.get("/api/account").json()["kind"] == "agent"
    assert bot.get("/api/account").json()["timezone"] == "America/Chicago"
    changed = ada.patch("/api/account", json={"timezone": "Europe/Paris"})
    assert changed.json()["timezone"] == "Europe/Paris"
    refused = bot.patch("/api/account", json={"timezone": "UTC"})
    assert refused.status_code == 403 and refused.json()["error"] == "forbidden"
    invalid = ada.patch("/api/account", json={"timezone": "Mars/Olympus"})
    assert (invalid.status_code, invalid.json()["message"]) == (422, "timezone is not a known name")


def test_the_person_manages_agents_by_name(app, ada):
    added = ada.post("/api/agents", json={"name": "Bot"})
    assert added.status_code == 201
    assert added.json() == {"name": "Bot", "retired_at": None, "key_expires_at": None}
    issued = ada.post("/api/agents/Bot/key", json={})
    assert issued.status_code == 201 and UTC_TEXT.match(issued.json()["key_expires_at"])
    rolled = ada.post("/api/agents/Bot/key/roll", json={})
    assert rolled.status_code == 201 and rolled.json()["key"] != issued.json()["key"]
    assert ada.get("/api/agents").json()[0]["key_expires_at"] == rolled.json()["key_expires_at"]
    retired = ada.post("/api/agents/Bot/retire", json={}).json()
    assert UTC_TEXT.match(retired["retired_at"]) and retired["key_expires_at"] is None
    ada.post("/api/agents", json={"name": "Spare"})
    assert ada.request("DELETE", "/api/agents/Spare", json={}).status_code == 204
    assert [agent["name"] for agent in ada.get("/api/agents").json()] == ["Bot"]
    taken = ada.post("/api/agents", json={"name": "bot"})
    assert (taken.status_code, taken.json()["error"]) == (409, "conflict")


def test_an_agent_with_attached_work_is_reassigned_or_retired(app, ada):
    worker = _agent(app, ada, "Worker")
    task = worker.post("/api/tasks", json={"title": "Draft"}).json()
    attached = ada.request("DELETE", "/api/agents/Worker", json={})
    assert attached.status_code == 409 and attached.json()["error"] == "attached"
    assert ada.post("/api/agents/Worker/reassign", json={}).status_code == 204
    moved = ada.get(f"/api/tasks/{task['id']}").json()
    assert moved["created_by"] == {"name": "Ada", "kind": "person"}
    assert ada.get("/api/agents").json() == []


def test_an_agent_is_refused_every_person_only_route(bot):
    calls = (
        ("get", "/api/agents", None),
        ("post", "/api/agents", {"name": "Other"}),
        ("post", "/api/agents/Bot/key", None),
        ("post", "/api/agents/Bot/key/roll", None),
        ("post", "/api/agents/Bot/retire", None),
        ("post", "/api/agents/Bot/reassign", None),
        ("delete", "/api/agents/Bot", None),
        ("patch", "/api/account", {"timezone": "UTC"}),
    )
    for method, path, body in calls:
        response = getattr(bot, method)(path, **({"json": body} if body else {}))
        assert (response.status_code, response.json()["error"]) == (403, "forbidden"), path


def test_tasks_are_created_read_listed_and_named(ada, bot):
    created = ada.post(
        "/api/tasks",
        json={"title": "  Call   Chad ", "tags": "Deal:Chad", "due_at": "2026-07-01T17:00:00"},
    )
    assert created.status_code == 201
    task = created.json()
    assert (task["title"], task["tags"], task["notes"]) == ("Call Chad", ["deal:chad"], "")
    # A due_at without an offset is read in the account's timezone, America/Chicago.
    assert task["due_at"] == "2026-07-01T22:00:00.000Z"
    assert task["created_by"] == {"name": "Ada", "kind": "person"}
    child = bot.post("/api/tasks", json={"title": "Step", "parent_id": task["id"]}).json()
    assert child["created_by"] == {"name": "Bot", "kind": "agent"}
    assert [t["id"] for t in ada.get("/api/tasks").json()] == [task["id"]]
    assert [t["id"] for t in ada.get(f"/api/tasks?parent={task['id']}").json()] == [child["id"]]
    assert [t["id"] for t in ada.get("/api/tasks/tree").json()] == [task["id"], child["id"]]
    for body in (task, child, ada.get("/api/tasks/tree").json()):
        _ids_absent(body)
    for moment in (task["created_at"], task["updated_at"]):
        assert UTC_TEXT.match(moment)


def test_a_filtered_tree_keeps_ancestors_unmatched(ada):
    parent = ada.post("/api/tasks", json={"title": "Deal", "status": 2}).json()
    child = ada.post("/api/tasks", json={"title": "Sign", "parent_id": parent["id"]}).json()
    tree = ada.get("/api/tasks/tree?open=true").json()
    assert [(t["id"], t["matched"]) for t in tree] == [(parent["id"], False), (child["id"], True)]
    assert ada.get("/api/tasks?open=true").json() == []
    ada.patch(f"/api/tasks/{child['id']}", json={"tags": "deal:chad"})
    tagged = ada.get("/api/tasks/tree?tags=deal:chad").json()
    assert [t["id"] for t in tagged] == [parent["id"], child["id"]]


def test_a_patch_changes_what_it_sends_and_clears_a_null(ada, bot):
    task = ada.post(
        "/api/tasks",
        json={"title": "Call Chad", "notes": "First.", "tags": "deal:chad", "estimate_minutes": 30},
    ).json()
    patched = ada.patch(f"/api/tasks/{task['id']}", json={"status": 1, "estimate_minutes": None})
    body = patched.json()
    assert (body["status"], body["estimate_minutes"], body["notes"], body["tags"]) == (
        1,
        None,
        "First.",
        ["deal:chad"],
    )
    added = bot.patch(
        f"/api/tasks/{task['id']}",
        json={"append_notes": "Bot's note.", "add_tags": "Waiting", "remove_tags": "deal:chad"},
    ).json()
    assert added["notes"] == "First.\n\nBot's note." and added["tags"] == ["waiting"]
    assert added["updated_by"] == {"name": "Bot", "kind": "agent"}


def test_a_stale_updated_at_is_a_conflict(ada, bot):
    task = ada.post("/api/tasks", json={"title": "Call Chad"}).json()
    ada.patch(f"/api/tasks/{task['id']}", json={"title": "Call Chad back"})
    stale = bot.patch(
        f"/api/tasks/{task['id']}",
        json={"status": 2, "expected_updated_at": task["updated_at"]},
    )
    assert stale.status_code == 409 and stale.json() == {
        "error": "conflict",
        "message": "task changed since it was read",
    }
    current = ada.get(f"/api/tasks/{task['id']}").json()["updated_at"]
    done = bot.patch(f"/api/tasks/{task['id']}", json={"status": 2, "expected_updated_at": current})
    assert done.json()["status"] == 2


def test_a_drop_records_its_reason_and_delete_removes_the_subtree(ada):
    task = ada.post("/api/tasks", json={"title": "Deal"}).json()
    child = ada.post("/api/tasks", json={"title": "Step", "parent_id": task["id"]}).json()
    dropped = ada.post(f"/api/tasks/{task['id']}/drop", json={"reason": "Fell through."}).json()
    assert (dropped["status"], dropped["dropped_reason"]) == (3, "Fell through.")
    assert ada.get(f"/api/tasks/{child['id']}").json()["effectively_dropped"]
    assert ada.request("DELETE", f"/api/tasks/{task['id']}", json={}).status_code == 204
    gone = ada.get(f"/api/tasks/{child['id']}")
    assert (gone.status_code, gone.json()["error"]) == (404, "not_found")


def test_history_names_each_editor_in_write_order(ada, bot):
    task = ada.post("/api/tasks", json={"title": "Call Chad"}).json()
    bot.patch(f"/api/tasks/{task['id']}", json={"title": "Call Chad back"})
    history = ada.get(f"/api/tasks/{task['id']}/history").json()
    assert [entry["editor"]["name"] for entry in history] == ["Ada", "Bot"]
    assert [entry["state"]["title"] for entry in history] == ["Call Chad", "Call Chad back"]
    assert all(UTC_TEXT.match(entry["created_at"]) for entry in history)
    _ids_absent(history)


def test_a_duplicate_warns_and_a_confirmation_creates_it(ada):
    first = ada.post("/api/tasks", json={"title": "Pay rent"}).json()
    early = ada.post("/api/tasks", json={"title": "Pay rent", "allow_duplicate": True})
    assert (early.status_code, early.json()["error"]) == (409, "conflict")
    warned = ada.post("/api/tasks", json={"title": "pay RENT"})
    assert warned.status_code == 409
    assert warned.json()["error"] == "duplicate"
    assert warned.json()["task"] == {
        "id": first["id"],
        "title": "Pay rent",
        "status": 0,
        "due_at": None,
    }
    confirmed = ada.post("/api/tasks", json={"title": "pay RENT", "allow_duplicate": True})
    assert confirmed.status_code == 201


def test_blocks_are_added_edited_released_and_hold_the_status(ada, bot):
    task = ada.post("/api/tasks", json={"title": "Call Chad"}).json()
    block = bot.post(f"/api/tasks/{task['id']}/blocks", json={"reason": "Waiting on Chad"})
    assert block.status_code == 201
    block_id = block.json()["id"]
    assert block.json()["created_by"] == {"name": "Bot", "kind": "agent"}
    held = ada.patch(f"/api/tasks/{task['id']}", json={"status": 2})
    assert held.status_code == 409 and held.json()["message"] == "task is blocked"
    edited = ada.patch(f"/api/blocks/{block_id}", json={"reason": "Waiting on Sam"})
    assert edited.json()["reason"] == "Waiting on Sam"
    released = ada.post(f"/api/blocks/{block_id}/release", json={}).json()
    assert released["released_by"] == {"name": "Ada", "kind": "person"}
    assert UTC_TEXT.match(released["released_at"])
    listed = ada.get(f"/api/tasks/{task['id']}/blocks").json()
    assert [b["id"] for b in listed] == [block_id]
    _ids_absent(listed)
    assert ada.patch(f"/api/tasks/{task['id']}", json={"status": 2}).json()["status"] == 2


def test_a_close_can_release_its_blocks_first(ada):
    task = ada.post("/api/tasks", json={"title": "Call Chad"}).json()
    ada.post(f"/api/tasks/{task['id']}/blocks", json={"reason": "Waiting"})
    done = ada.patch(f"/api/tasks/{task['id']}", json={"status": 2, "release_blocks": True})
    assert (done.json()["status"], done.json()["blocked"]) == (2, False)


def test_another_account_is_not_found_on_every_task_block_and_agent_route(ada, bea):
    task = ada.post("/api/tasks", json={"title": "Mine"}).json()
    block = ada.post(f"/api/tasks/{task['id']}/blocks", json={"reason": "Waiting"}).json()
    ada.post("/api/agents", json={"name": "Bot"})
    task_path, block_path = f"/api/tasks/{task['id']}", f"/api/blocks/{block['id']}"
    calls = (
        ("get", task_path, None),
        ("patch", task_path, {"title": "Taken"}),
        ("delete", task_path, None),
        ("post", f"{task_path}/drop", {}),
        ("get", f"{task_path}/history", None),
        ("get", f"/api/tasks?parent={task['id']}", None),
        ("get", f"/api/tasks/tree?parent={task['id']}", None),
        ("post", "/api/tasks", {"title": "Nested", "parent_id": task["id"]}),
        ("get", f"{task_path}/blocks", None),
        ("post", f"{task_path}/blocks", {"reason": "Taken"}),
        ("patch", block_path, {"reason": "Taken"}),
        ("post", f"{block_path}/release", {}),
        ("post", "/api/agents/Bot/key", {}),
        ("post", "/api/agents/Bot/retire", {}),
        ("post", "/api/agents/Bot/reassign", {}),
        ("delete", "/api/agents/Bot", None),
    )
    for method, path, body in calls:
        # A cookie write sends JSON, so a body-less write sends an empty object.
        kwargs = {"json": body if body is not None else {}} if method != "get" else {}
        response = bea.request(method.upper(), path, **kwargs)
        assert (response.status_code, response.json()["error"]) == (404, "not_found"), path
    assert bea.get("/api/tasks").json() == []
    assert ada.get(task_path).json()["title"] == "Mine"


def test_every_error_kind_has_its_status_and_body(ada):
    unknown = ada.get("/api/nowhere")
    assert (unknown.status_code, unknown.json()["error"]) == (404, "not_found")
    missing = ada.post("/api/tasks", json={})
    assert (missing.status_code, missing.json()) == (
        422,
        {"error": "invalid", "message": "title: Field required"},
    )
    misspelled = ada.post("/api/tasks", json={"title": "x", "tittle": "y"})
    assert (misspelled.status_code, misspelled.json()["error"]) == (422, "invalid")
    not_an_int = ada.post("/api/tasks", json={"title": "x", "status": True})
    assert not_an_int.status_code == 422
    blank = ada.post("/api/tasks", json={"title": " "})
    assert blank.json() == {"error": "invalid", "message": "title is required"}
    wrong_method = ada.put("/api/tasks", json={})
    assert (wrong_method.status_code, wrong_method.json()["error"]) == (405, "invalid")


def test_the_openapi_schema_declares_every_route():
    schema = create_app(load_settings({})).openapi()
    assert len(schema["paths"]) == 18
    assert "TaskOut" in schema["components"]["schemas"]


def test_a_datetime_with_an_offset_comes_back_in_utc(ada):
    sent = (datetime(2026, 11, 6, 17, tzinfo=UTC) - timedelta(hours=0)).isoformat()
    task = ada.post("/api/tasks", json={"title": "Offset", "due_at": sent}).json()
    assert task["due_at"] == "2026-11-06T17:00:00.000Z"


def test_the_apps_lifespan_serves_mcp_beside_the_api(app, ada):
    ada.post("/api/agents", json={"name": "Bot"})
    key = ada.post("/api/agents/Bot/key", json={}).json()["key"]
    token = _client(app).post("/api/exchange", json={"key": key}).json()["token"]
    # Entering the client runs the app's lifespan, which starts the MCP session manager.
    with TestClient(app, base_url="https://testserver") as served:
        response = served.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={
                "Authorization": f"Bearer {token}",
                "accept": "application/json, text/event-stream",
            },
        )
    assert response.status_code == 200
    assert "create_task" in {tool["name"] for tool in response.json()["result"]["tools"]}


def test_a_read_does_not_wait_for_a_writer(ada):
    ada.post("/api/tasks", json={"title": "Call Chad"})
    with db.open_session() as writer:
        writer.connection()
        response = ada.get("/api/tasks")
    assert response.status_code == 200 and len(response.json()) == 1


def test_the_password_check_does_not_hold_the_write_lock(app, monkeypatch):
    checked = auth._passwords.verify

    def verify_while_another_call_writes(password: str, stored: str) -> bool:
        # Another call takes the write lock while the slow check runs.
        with db.open_session() as writer:
            writer.connection()
        return checked(password, stored)

    monkeypatch.setattr(auth._passwords, "verify", verify_while_another_call_writes)
    response = _client(app).post(
        "/api/login", json={"email": "ada@example.com", "password": "password-1"}
    )
    assert response.status_code == 204


def test_an_id_or_time_out_of_range_is_refused_with_the_error_body(ada):
    for path in ("/api/tasks/99999999999999999999", f"/api/tasks/{2**70}/history"):
        response = ada.get(path)
        assert (response.status_code, response.json()["error"]) == (404, "not_found"), path
    created = ada.post("/api/tasks", json={"title": "Child", "parent_id": 2**70})
    assert (created.status_code, created.json()["error"]) == (404, "not_found")
    far = ada.post("/api/tasks", json={"title": "Far", "due_at": "9999-12-31T23:00:00-05:00"})
    assert (far.status_code, far.json()) == (
        422,
        {"error": "invalid", "message": "time is out of range"},
    )


def test_an_agent_is_addressed_by_its_name_in_any_case(ada):
    assert ada.post("/api/agents", json={"name": "Bot"}).status_code == 201
    assert ada.post("/api/agents/bot/key", json={}).status_code == 201
    slash = ada.post("/api/agents", json={"name": "ops/bot"})
    assert (slash.status_code, slash.json()["error"]) == (422, "invalid")
