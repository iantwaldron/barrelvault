"""The task service: create, read, update, and delete, inside the account."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from barrelvault.account import account_of, add_agent
from barrelvault.errors import Conflict, Invalid, NotFound
from barrelvault.models import Block, Task, TaskHistory, User
from barrelvault.status import COMPLETED, DROPPED, IN_PROGRESS, TODO
from barrelvault.tasks import (
    create_task,
    delete_task,
    get_task,
    list_tasks,
    task_history,
    update_task,
)
from tests.support import act_as, make_account, second_account

DUE = datetime(2026, 11, 6, 23, tzinfo=UTC)


@pytest.fixture
def ada(session) -> User:
    _human, secret = make_account(session)
    return act_as(session, secret)


@pytest.fixture
def bot(session, ada) -> User:
    agent = add_agent(session, ada, "Bot")
    return session.get(User, agent.user_id)


def _block(session: Session, task_id: int, by: User) -> Block:
    block = Block(task_id=task_id, reason="Waiting on Chad", created_by=by.id, created_at=DUE)
    session.add(block)
    session.flush()
    return block


def test_a_create_by_the_person_and_by_an_agent_is_owned_by_the_person(session, ada, bot):
    mine = create_task(session, ada, "Call Chad")
    theirs = create_task(session, bot, "Draft the follow-up")
    assert (mine.owned_by, mine.created_by, mine.updated_by) == (ada.id, ada.id, ada.id)
    assert (theirs.owned_by, theirs.created_by, theirs.updated_by) == (ada.id, bot.id, bot.id)
    assert mine.updated_at == mine.created_at


@pytest.mark.parametrize("acting", ["ada", "bot"])
def test_another_account_is_not_found(session, ada, bot, acting):
    actor = {"ada": ada, "bot": bot}[acting]
    _bea_id, bea_secret = second_account(session)
    bea = act_as(session, bea_secret)
    beas = create_task(session, bea, "Bea's task")
    mine = create_task(session, ada, "Mine")
    calls = (
        lambda: get_task(session, actor, beas.id),
        lambda: update_task(session, actor, beas.id, title="Taken"),
        lambda: delete_task(session, actor, beas.id),
        lambda: task_history(session, actor, beas.id),
        lambda: list_tasks(session, actor, beas.id),
        lambda: create_task(session, actor, "Nested", parent_id=beas.id),
        lambda: update_task(session, actor, mine.id, parent_id=beas.id),
    )
    for call in calls:
        with pytest.raises(NotFound):
            call()
    assert get_task(session, bea, beas.id).title == "Bea's task"
    assert [task.id for task in list_tasks(session, actor)] == [mine.id]


def test_an_actor_with_no_profile_has_no_account(session):
    user = User(name="Nobody")
    session.add(user)
    session.flush()
    with pytest.raises(NotFound):
        account_of(session, user)


def test_owned_by_is_the_persons_and_never_changes(session, ada, bot):
    task = create_task(session, bot, "Draft")
    session.commit()
    for owner in (bot.id, 9999):
        with pytest.raises(IntegrityError, match="a task is owned by a human"):
            session.connection().execute(
                text(
                    "INSERT INTO task (title, created_by, created_at, updated_by, updated_at, "
                    "owned_by) VALUES ('x', :by, '2026-01-01', :by, '2026-01-01', :owner)"
                ),
                {"by": ada.id, "owner": owner},
            )
        session.rollback()
    with pytest.raises(IntegrityError, match="a task keeps its owner"):
        session.connection().execute(
            text("UPDATE task SET owned_by = :to WHERE id = :id"), {"to": bot.id, "id": task.id}
        )
    session.rollback()
    stored = session.get(Task, task.id)
    stored.owned_by = bot.id
    with pytest.raises(IntegrityError, match="a task keeps its owner"):
        session.flush()


def test_a_title_is_stored_trimmed_and_collapsed_and_fields_are_checked(session, ada):
    task = create_task(session, ada, "  Call   Chad\tback ")
    assert task.title == "Call Chad back"
    cases = (
        (Invalid, "title is required", lambda: create_task(session, ada, "   ")),
        (Invalid, "status is 0, 1, 2, or 3", lambda: create_task(session, ada, "x", status=4)),
        (Invalid, "status is 0, 1, 2, or 3", lambda: create_task(session, ada, "x", status=True)),
        (Invalid, "status is 0, 1, 2, or 3", lambda: create_task(session, ada, "x", status=1.0)),
        (
            Invalid,
            "estimate_minutes is from 0 through 32767",
            lambda: create_task(session, ada, "x", estimate_minutes=32768),
        ),
        (
            Invalid,
            "estimate_minutes is from 0 through 32767",
            lambda: create_task(session, ada, "x", estimate_minutes=5.5),
        ),
        (
            Invalid,
            "estimate_minutes is from 0 through 32767",
            lambda: update_task(session, ada, task.id, estimate_minutes=-1),
        ),
        (Invalid, "title is required", lambda: update_task(session, ada, task.id, title=" ")),
    )
    for kind, message, call in cases:
        with pytest.raises(kind) as raised:
            call()
        assert raised.value.message == message
    assert create_task(session, ada, "Estimated", estimate_minutes=0).estimate_minutes == 0


def test_due_at_is_stored_in_utc_and_read_in_the_account_timezone_without_an_offset(
    session, ada, bot
):
    offset = datetime(2026, 11, 6, 17, tzinfo=timezone(timedelta(hours=-6)))
    assert create_task(session, ada, "Offset", due_at=offset).due_at == DUE
    # The account is in America/Chicago, so 17:00 on 2026-07-01 is 22:00 UTC, for an agent too.
    naive = create_task(session, bot, "Naive", due_at=datetime(2026, 7, 1, 17))
    assert naive.due_at == datetime(2026, 7, 1, 22, tzinfo=UTC)
    moved = update_task(session, ada, naive.id, due_at=datetime(2026, 12, 1, 17))
    assert moved.due_at == datetime(2026, 12, 1, 23, tzinfo=UTC)
    assert update_task(session, ada, naive.id, due_at=None).due_at is None


def test_every_returned_datetime_is_in_utc(session, ada):
    task = create_task(session, ada, "Call Chad", due_at=datetime(2026, 7, 1, 17))
    for view in (task, get_task(session, ada, task.id), *list_tasks(session, ada)):
        for moment in (view.created_at, view.updated_at, view.due_at):
            assert moment.utcoffset() == timedelta(0)
    assert all(
        e.created_at.utcoffset() == timedelta(0) for e in task_history(session, ada, task.id)
    )


def test_service_timestamps_are_whole_milliseconds(session, ada):
    task = create_task(session, ada, "Call Chad")
    assert task.created_at.microsecond % 1000 == 0


def test_null_text_columns_read_as_empty(session, ada):
    session.connection().execute(
        text(
            "INSERT INTO task (title, notes, tags, created_by, created_at, updated_by, "
            "updated_at, owned_by) VALUES ('Raw', NULL, NULL, :by, :at, :by, :at, :by)"
        ),
        {"by": ada.id, "at": "2026-01-01 00:00:00.000000"},
    )
    raw_id = session.connection().execute(text("SELECT id FROM task")).scalar_one()
    detail = get_task(session, ada, raw_id)
    assert (detail.notes, detail.tags) == ("", [])
    [listed] = list_tasks(session, ada)
    assert (listed.notes, listed.tags) == ("", [])
    unchanged = update_task(session, ada, raw_id, notes="", tags="")
    assert unchanged.updated_at == detail.updated_at


def test_a_partial_update_leaves_the_other_fields(session, ada):
    parent = create_task(session, ada, "Project")
    task = create_task(
        session,
        ada,
        "Call Chad",
        parent_id=parent.id,
        notes="About the deal.",
        tags="deal:chad",
        estimate_minutes=30,
        due_at=DUE,
    )

    def kept(view) -> tuple:
        return (view.parent_id, view.notes, view.tags, view.estimate_minutes, view.due_at)

    by_status = update_task(session, ada, task.id, status=IN_PROGRESS)
    assert by_status.status == IN_PROGRESS and by_status.title == "Call Chad"
    assert kept(by_status) == (parent.id, "About the deal.", ["deal:chad"], 30, DUE)
    by_title = update_task(session, ada, task.id, title="Call Chad back")
    assert by_title.status == IN_PROGRESS
    assert kept(by_title) == (parent.id, "About the deal.", ["deal:chad"], 30, DUE)


def test_an_update_moves_updated_at_and_a_same_value_update_writes_nothing(session, ada, bot):
    task = create_task(session, ada, "Call Chad", tags="deal:chad")
    same = update_task(session, bot, task.id, title=" Call  Chad ", tags="DEAL:CHAD")
    assert (same.updated_at, same.updated_by) == (task.updated_at, ada.id)
    tagged = update_task(session, bot, task.id, tags="deal:chad urgent")
    assert tagged.updated_at > task.updated_at and tagged.updated_by == bot.id
    entries = task_history(session, ada, task.id)
    assert len(entries) == 2 and entries[-1].state["tags"] == "deal:chad urgent"


def test_any_status_change_is_allowed_including_dropped_and_back(session, ada):
    task = create_task(session, ada, "Call Chad")
    for status in (IN_PROGRESS, COMPLETED, DROPPED, TODO):
        assert update_task(session, ada, task.id, status=status).status == status


def test_a_blocked_task_keeps_its_status_in_either_direction(session, ada):
    task = create_task(session, ada, "Call Chad", status=IN_PROGRESS)
    _block(session, task.id, ada)
    for status in (TODO, COMPLETED, DROPPED):
        with pytest.raises(Conflict) as raised:
            update_task(session, ada, task.id, status=status)
        assert raised.value.message == "task is blocked"
    assert get_task(session, ada, task.id).status == IN_PROGRESS
    assert update_task(session, ada, task.id, title="Call Chad back").title == "Call Chad back"


def test_history_records_each_change_in_order(session, ada, bot):
    task = create_task(session, ada, "Call Chad", due_at=DUE)
    update_task(session, bot, task.id, title="Call Chad back")
    update_task(session, ada, task.id, status=IN_PROGRESS)
    entries = task_history(session, ada, task.id)
    assert [entry.user_id for entry in entries] == [ada.id, bot.id, ada.id]
    newest = get_task(session, ada, task.id)
    assert entries[-1].state == {
        "parent_id": newest.parent_id,
        "title": newest.title,
        "notes": newest.notes,
        "tags": " ".join(newest.tags),
        "status": newest.status,
        "estimate_minutes": newest.estimate_minutes,
        "due_at": newest.due_at.isoformat(),
    }


def test_a_list_is_one_level_in_created_order(session, ada):
    first = create_task(session, ada, "First")
    second = create_task(session, ada, "Second")
    child = create_task(session, ada, "Child", parent_id=first.id)
    assert [task.id for task in list_tasks(session, ada)] == [first.id, second.id]
    assert [task.id for task in list_tasks(session, ada, first.id)] == [child.id]
    assert list_tasks(session, ada, second.id) == []


def test_nesting_refuses_a_cycle_and_a_missing_parent_and_detach_keeps_children(session, ada):
    root = create_task(session, ada, "Root")
    middle = create_task(session, ada, "Middle", parent_id=root.id)
    leaf = create_task(session, ada, "Leaf", parent_id=middle.id)
    for parent in (root.id, middle.id, leaf.id):
        with pytest.raises(Invalid):
            update_task(session, ada, root.id, parent_id=parent)
    with pytest.raises(NotFound):
        update_task(session, ada, root.id, parent_id=9999)
    with pytest.raises(NotFound):
        create_task(session, ada, "Orphan", parent_id=9999)
    detached = update_task(session, ada, middle.id, parent_id=None)
    assert detached.parent_id is None
    assert [task.id for task in list_tasks(session, ada, middle.id)] == [leaf.id]
    assert [task.id for task in list_tasks(session, ada)] == [root.id, middle.id]


def test_a_delete_takes_the_subtree_and_its_history(session, ada):
    root = create_task(session, ada, "Root")
    child = create_task(session, ada, "Child", parent_id=root.id)
    keep = create_task(session, ada, "Keep")
    stale_child = session.get(Task, child.id)
    delete_task(session, ada, root.id)
    assert stale_child not in session
    assert session.get(Task, child.id) is None
    assert [task.id for task in list_tasks(session, ada)] == [keep.id]
    assert {row.task_id for row in session.exec(select(TaskHistory))} == {keep.id}


def test_a_failed_flush_drops_the_task_change_and_its_snapshot(session, ada):
    task = create_task(session, ada, "Call Chad")
    other = create_task(session, ada, "Other")
    session.commit()
    stored, broken = session.get(Task, task.id), session.get(Task, other.id)
    stored.title = "Changed"
    broken.estimate_minutes = -1
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()
    assert get_task(session, ada, task.id).title == "Call Chad"
    assert len(task_history(session, ada, task.id)) == 1


def test_open_for_counts_from_creation(session, ada):
    task = create_task(session, ada, "Call Chad")
    stored = session.get(Task, task.id)
    assert stored.open_for(task.created_at + timedelta(minutes=5)) == timedelta(minutes=5)


def test_updated_at_moves_on_every_change_even_inside_one_millisecond(session, ada, monkeypatch):
    task = create_task(session, ada, "Call Chad")
    monkeypatch.setattr("barrelvault.tasks.now", lambda: task.updated_at)
    first = update_task(session, ada, task.id, title="One")
    second = update_task(session, ada, task.id, title="Two")
    assert task.updated_at < first.updated_at < second.updated_at
