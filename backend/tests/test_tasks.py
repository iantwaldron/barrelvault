"""The task service: account boundary, updates, dropped, nesting, delete, duplicates."""

import threading
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, select

from barrelvault import db
from barrelvault.account import account_of, add_agent
from barrelvault.errors import Conflict, Duplicate, Invalid, NotFound
from barrelvault.models import Block, Task, TaskHistory, User
from barrelvault.status import COMPLETED, DROPPED, IN_PROGRESS, TODO
from barrelvault.tasks import (
    create_task,
    delete_task,
    drop_task,
    get_task,
    list_tasks,
    task_history,
    task_tree,
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


def test_a_create_by_the_person_and_by_an_agent_is_owned_by_the_person(session, ada, bot):
    mine = create_task(session, ada, "Call Chad")
    theirs = create_task(session, bot, "Draft the follow-up")
    assert (mine.owned_by, mine.created_by, mine.updated_by) == (ada.id, ada.id, ada.id)
    assert (theirs.owned_by, theirs.created_by, theirs.updated_by) == (ada.id, bot.id, bot.id)
    assert mine.updated_at == mine.created_at


def test_another_account_is_not_found(session, ada):
    _bea_id, bea_secret = second_account(session)
    bea = act_as(session, bea_secret)
    beas = create_task(session, bea, "Bea's task")
    mine = create_task(session, ada, "Mine")
    calls = (
        lambda: get_task(session, ada, beas.id),
        lambda: update_task(session, ada, beas.id, title="Taken"),
        lambda: drop_task(session, ada, beas.id),
        lambda: delete_task(session, ada, beas.id),
        lambda: task_history(session, ada, beas.id),
        lambda: list_tasks(session, ada, beas.id),
        lambda: task_tree(session, ada, beas.id),
        lambda: create_task(session, ada, "Nested", parent_id=beas.id),
        lambda: update_task(session, ada, mine.id, parent_id=beas.id),
    )
    for call in calls:
        with pytest.raises(NotFound):
            call()
    assert get_task(session, bea, beas.id).title == "Bea's task"
    assert [task.id for task in list_tasks(session, ada)] == [mine.id]


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
                    "INSERT INTO task (title, title_key, created_by, created_at, updated_by, "
                    "updated_at, owned_by) "
                    "VALUES ('x', 'x', :by, '2026-01-01', :by, '2026-01-01', :owner)"
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
            lambda: create_task(session, ada, "x", estimate_minutes=5.5),
        ),
        (
            Invalid,
            "estimate_minutes is from 0 through 32767",
            lambda: create_task(session, ada, "x", estimate_minutes=32768),
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
    with_offset = create_task(
        session,
        ada,
        "Offset",
        due_at=datetime(2026, 11, 6, 17, tzinfo=timezone(timedelta(hours=-6))),
    )
    assert with_offset.due_at == DUE
    # The account is in America/Chicago, so 17:00 on 2026-07-01 is 22:00 UTC, for an agent too.
    naive = create_task(session, bot, "Naive", due_at=datetime(2026, 7, 1, 17))
    assert naive.due_at == datetime(2026, 7, 1, 22, tzinfo=UTC)
    moved = update_task(session, ada, naive.id, due_at=datetime(2026, 12, 1, 17))
    assert moved.due_at == datetime(2026, 12, 1, 23, tzinfo=UTC)
    assert update_task(session, ada, naive.id, due_at=None).due_at is None


def test_null_text_columns_read_as_empty(session, ada):
    session.connection().execute(
        text(
            "INSERT INTO task (title, title_key, notes, tags, created_by, created_at, "
            "updated_by, updated_at, owned_by) "
            "VALUES ('Raw', 'raw', NULL, NULL, :by, :at, :by, :at, :by)"
        ),
        {"by": ada.id, "at": "2026-01-01 00:00:00.000000"},
    )
    raw_id = session.connection().execute(text("SELECT id FROM task")).scalar_one()
    detail = get_task(session, ada, raw_id)
    assert (detail.notes, detail.tags) == ("", [])
    [summary] = list_tasks(session, ada)
    assert summary.tags == []
    assert list_tasks(session, ada, tags="home") == []
    unchanged = update_task(session, ada, raw_id, notes="")
    assert unchanged.updated_at == detail.updated_at
    assert update_task(session, ada, raw_id, add="Home").tags == ["home"]
    assert [task.id for task in list_tasks(session, ada, tags="home")] == [raw_id]


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

    def kept(detail) -> tuple:
        return (
            detail.parent_id,
            detail.notes,
            detail.tags,
            detail.estimate_minutes,
            detail.due_at,
        )

    by_status = update_task(session, ada, task.id, status=IN_PROGRESS)
    assert by_status.status == IN_PROGRESS and by_status.title == "Call Chad"
    assert kept(by_status) == (parent.id, "About the deal.", ["deal:chad"], 30, DUE)
    by_title = update_task(session, ada, task.id, title="Call Chad back")
    assert by_title.status == IN_PROGRESS
    assert kept(by_title) == (parent.id, "About the deal.", ["deal:chad"], 30, DUE)


def test_an_append_and_a_tag_add_keep_a_change_made_after_the_read(session, ada, bot):
    task = create_task(session, ada, "Call Chad", notes="First.", tags="deal:chad")
    read_by_bot = get_task(session, bot, task.id)
    update_task(session, ada, task.id, notes="First, edited.", tags="deal:chad urgent")
    update_task(session, bot, read_by_bot.id, append_notes="Bot's summary.", add="Waiting")
    stored = get_task(session, ada, task.id)
    assert stored.notes == "First, edited.\n\nBot's summary."
    assert stored.tags == ["deal:chad", "urgent", "waiting"]
    removed = update_task(session, ada, task.id, remove="URGENT waiting")
    assert removed.tags == ["deal:chad"]
    assert (
        update_task(session, ada, create_task(session, ada, "Empty").id, append_notes="Only.").notes
        == "Only."
    )


def test_a_field_and_its_operation_are_not_sent_together(session, ada):
    task = create_task(session, ada, "Call Chad")
    with pytest.raises(Invalid):
        update_task(session, ada, task.id, notes="x", append_notes="y")
    with pytest.raises(Invalid):
        update_task(session, ada, task.id, tags="x", add="y")


def test_a_stale_updated_at_is_refused_and_a_current_one_applied(session, ada, bot):
    task = create_task(session, ada, "Call Chad")
    read = get_task(session, bot, task.id)
    update_task(session, ada, task.id, title="Call Chad today")
    with pytest.raises(Conflict) as raised:
        update_task(session, bot, task.id, status=COMPLETED, expected_updated_at=read.updated_at)
    assert raised.value.message == "task changed since it was read"
    assert get_task(session, ada, task.id).status == TODO
    current = get_task(session, bot, task.id)
    done = update_task(
        session, bot, task.id, status=COMPLETED, expected_updated_at=current.updated_at
    )
    assert done.status == COMPLETED
    with pytest.raises(Conflict):
        drop_task(session, bot, task.id, expected_updated_at=current.updated_at)


def test_a_change_that_leaves_the_task_as_it_was_writes_nothing(session, ada, bot):
    task = create_task(session, ada, "Call Chad", tags="deal:chad")
    same = update_task(session, bot, task.id, title=" Call  Chad ", add="DEAL:CHAD", remove="away")
    assert same.updated_at == task.updated_at
    assert same.updated_by == ada.id
    assert len(task_history(session, ada, task.id)) == 1


def test_history_reads_each_change_in_order(session, ada, bot):
    task = create_task(session, ada, "Call Chad", due_at=DUE)
    update_task(session, bot, task.id, title="Call Chad back")
    update_task(session, ada, task.id, status=IN_PROGRESS)
    entries = task_history(session, ada, task.id)
    assert [entry.state["title"] for entry in entries] == [
        "Call Chad",
        "Call Chad back",
        "Call Chad back",
    ]
    assert [entry.user_id for entry in entries] == [ada.id, bot.id, ada.id]
    assert entries[0].state["due_at"] == "2026-11-06T23:00:00+00:00"
    assert entries[-1].state["status"] == IN_PROGRESS
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


def test_lists_are_one_level_in_order_and_filter_by_tag_and_open(session, ada):
    first = create_task(session, ada, "First", tags="deal:chad home")
    second = create_task(session, ada, "Second", tags="deal:chad")
    child = create_task(session, ada, "Child", parent_id=first.id)
    done = create_task(session, ada, "Done", status=COMPLETED)
    assert [task.id for task in list_tasks(session, ada)] == [first.id, second.id, done.id]
    assert [task.id for task in list_tasks(session, ada, first.id)] == [child.id]
    assert [task.id for task in list_tasks(session, ada, tags="HOME deal:chad")] == [first.id]
    assert list_tasks(session, ada, tags="deal") == []
    assert [task.id for task in list_tasks(session, ada, open_only=True)] == [first.id, second.id]
    assert [task.id for task in task_tree(session, ada)] == [first.id, child.id, second.id, done.id]


def test_a_dropped_parent_makes_its_subtree_effectively_dropped(session, ada):
    parent = create_task(session, ada, "Project")
    child = create_task(session, ada, "Step", parent_id=parent.id, status=IN_PROGRESS)
    drop_task(session, ada, parent.id)
    under = get_task(session, ada, child.id)
    assert under.effectively_dropped and under.status == IN_PROGRESS
    assert task_tree(session, ada, open_only=True) == []
    update_task(session, ada, parent.id, status=TODO)
    assert not get_task(session, ada, child.id).effectively_dropped
    drop_task(session, ada, parent.id)
    update_task(session, ada, child.id, parent_id=None)
    assert not get_task(session, ada, child.id).effectively_dropped
    update_task(session, ada, child.id, parent_id=parent.id)
    assert get_task(session, ada, child.id).effectively_dropped


def test_completing_a_parent_leaves_its_children_open(session, ada):
    parent = create_task(session, ada, "Project")
    child = create_task(session, ada, "Step", parent_id=parent.id)
    update_task(session, ada, parent.id, status=COMPLETED)
    remaining = get_task(session, ada, child.id)
    assert remaining.status == TODO and not remaining.effectively_dropped
    opened = task_tree(session, ada, open_only=True)
    assert [(task.id, task.matched) for task in opened] == [(parent.id, False), (child.id, True)]


def test_a_drop_records_its_reason_in_notes(session, ada):
    task = create_task(session, ada, "Call Chad", notes="Context.")
    dropped = drop_task(session, ada, task.id, "Superseded by the Q3 plan.")
    assert dropped.status == DROPPED
    assert dropped.dropped_reason == "Superseded by the Q3 plan."
    assert dropped.notes == "Context.\n\n```dropped\nSuperseded by the Q3 plan.\n```"
    again = drop_task(session, ada, task.id, "Deal fell through.")
    assert again.dropped_reason == "Deal fell through."
    reopened = update_task(session, ada, task.id, status=TODO)
    assert reopened.dropped_reason == ""
    assert "Deal fell through." in reopened.notes
    plain = drop_task(session, ada, create_task(session, ada, "Plain").id)
    assert (plain.dropped_reason, plain.notes) == ("", "")


def test_a_block_shaped_drop_reason_is_returned_as_text(session, ada):
    reason = "See:\n```dropped\nfake\n```"
    task = create_task(session, ada, "Call Chad")
    assert drop_task(session, ada, task.id, reason).dropped_reason == reason


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
    assert [task.id for task in task_tree(session, ada, middle.id)] == [leaf.id]
    assert [task.id for task in list_tasks(session, ada)] == [root.id, middle.id]


def test_a_delete_takes_the_subtree_and_its_history(session, ada):
    root = create_task(session, ada, "Root")
    child = create_task(session, ada, "Child", parent_id=root.id)
    keep = create_task(session, ada, "Keep")
    stale_child = session.get(Task, child.id)
    delete_task(session, ada, root.id)
    assert stale_child not in session
    assert [task.id for task in task_tree(session, ada)] == [keep.id]
    assert session.get(Task, child.id) is None
    assert {row.task_id for row in session.exec(select(TaskHistory))} == {keep.id}


def test_a_duplicate_is_refused_with_the_existing_tasks_fields(session, ada, bot):
    first = create_task(session, ada, "Pay rent", due_at=DUE)
    with pytest.raises(Duplicate) as raised:
        create_task(session, bot, "  pay   RENT ", due_at=DUE)
    match = raised.value
    assert (match.task_id, match.title, match.status, match.due_at) == (
        first.id,
        "Pay rent",
        TODO,
        DUE,
    )
    for status in (COMPLETED, DROPPED):
        update_task(session, ada, first.id, status=status)
        with pytest.raises(Duplicate):
            create_task(session, ada, "Pay rent", due_at=DUE)
    assert len(task_tree(session, ada)) == 1
    assert create_task(session, ada, "Pay rent", due_at=DUE, allow_duplicate=True).id != first.id


def test_an_undated_duplicate_matches_only_an_open_undated_task(session, ada):
    first = create_task(session, ada, "Call mom")
    with pytest.raises(Duplicate):
        create_task(session, ada, "call mom")
    update_task(session, ada, first.id, status=COMPLETED)
    assert create_task(session, ada, "Call mom").id != first.id


def test_a_different_due_at_or_another_account_is_not_a_duplicate(session, ada):
    create_task(session, ada, "Pay rent", due_at=DUE)
    assert create_task(session, ada, "Pay rent", due_at=DUE + timedelta(minutes=1))
    assert create_task(session, ada, "Pay rent")
    _bea_id, bea_secret = second_account(session)
    assert create_task(session, act_as(session, bea_secret), "Pay rent", due_at=DUE)


def test_two_simultaneous_creates_leave_one_task(database):
    SQLModel.metadata.create_all(db.get_engine())
    with db.open_session() as session:
        _human, secret = make_account(session)
    start = threading.Event()
    outcomes: list[object] = []

    def create() -> None:
        start.wait(timeout=5)
        try:
            with db.open_session() as session:
                create_task(session, act_as(session, secret), "Pay rent", due_at=DUE)
            outcomes.append("created")
        except Duplicate as duplicate:
            outcomes.append(duplicate)

    threads = [threading.Thread(target=create) for _ in range(2)]
    for thread in threads:
        thread.start()
    start.set()
    for thread in threads:
        thread.join(timeout=10)
    assert outcomes.count("created") == 1
    assert sum(isinstance(outcome, Duplicate) for outcome in outcomes) == 1
    with db.open_session(immediate=False) as session:
        assert len(session.exec(select(Task)).all()) == 1


def test_open_for_counts_from_creation(session, ada):
    task = create_task(session, ada, "Call Chad")
    stored = session.get(Task, task.id)
    assert stored.open_for(task.created_at + timedelta(minutes=5)) == timedelta(minutes=5)


def _block(session: Session, task_id: int, by: User) -> Block:
    block = Block(task_id=task_id, reason="Waiting on Chad", created_by=by.id, created_at=DUE)
    session.add(block)
    session.flush()
    return block


def test_a_blocked_task_keeps_its_status_in_either_direction(session, ada):
    task = create_task(session, ada, "Call Chad", status=IN_PROGRESS)
    _block(session, task.id, ada)
    for status in (TODO, COMPLETED, DROPPED):
        with pytest.raises(Conflict) as raised:
            update_task(session, ada, task.id, status=status)
        assert raised.value.message == "task is blocked"
    with pytest.raises(Conflict):
        drop_task(session, ada, task.id)
    with pytest.raises(Conflict):
        update_task(session, ada, task.id, status=TODO, release_blocks=True)
    detail = get_task(session, ada, task.id)
    assert detail.status == IN_PROGRESS and detail.blocked
    assert update_task(session, ada, task.id, title="Call Chad back").title == "Call Chad back"


def test_a_close_can_release_the_open_blocks_first(session, ada, bot):
    task = create_task(session, ada, "Call Chad")
    first, second = _block(session, task.id, ada), _block(session, task.id, ada)
    done = update_task(session, bot, task.id, status=COMPLETED, release_blocks=True)
    assert done.status == COMPLETED and not done.blocked
    for block in (first, second):
        session.refresh(block)
        assert block.released_by == bot.id and block.released_at is not None
    other = create_task(session, ada, "Deal")
    _block(session, other.id, ada)
    dropped = drop_task(session, bot, other.id, "Deal fell through.", release_blocks=True)
    assert dropped.status == DROPPED and dropped.dropped_reason == "Deal fell through."


def test_a_duplicate_title_matches_without_case_in_any_script(session, ada):
    create_task(session, ada, "Café rent", due_at=DUE)
    for title in ("CAFÉ RENT", "café  rent"):
        with pytest.raises(Duplicate):
            create_task(session, ada, title, due_at=DUE)
    create_task(session, ada, "Straße")
    with pytest.raises(Duplicate):
        create_task(session, ada, "STRASSE")


def test_a_drop_after_an_unclosed_fence_keeps_its_reason(session, ada):
    task = create_task(session, ada, "Call Chad", notes="Script:\n```\nnever closed")
    assert drop_task(session, ada, task.id, "superseded").dropped_reason == "superseded"


def test_a_blank_append_changes_nothing(session, ada):
    task = create_task(session, ada, "Call Chad", notes="hello")
    for blank in ("", "   ", "\n"):
        same = update_task(session, ada, task.id, append_notes=blank)
        assert same.notes == "hello" and same.updated_at == task.updated_at
    assert len(task_history(session, ada, task.id)) == 1


def test_an_updated_at_without_an_offset_is_read_in_the_account_timezone(session, ada):
    task = create_task(session, ada, "Call Chad")
    as_stored = task.updated_at.replace(tzinfo=None)
    with pytest.raises(Conflict):
        update_task(session, ada, task.id, title="UTC wall time", expected_updated_at=as_stored)
    chicago = task.updated_at.astimezone(ZoneInfo("America/Chicago")).replace(tzinfo=None)
    assert update_task(session, ada, task.id, title="Local", expected_updated_at=chicago)


def test_service_timestamps_are_whole_milliseconds(session, ada):
    task = create_task(session, ada, "Call Chad")
    assert task.created_at.microsecond % 1000 == 0
    echoed = datetime.fromisoformat(task.updated_at.isoformat(timespec="milliseconds"))
    assert update_task(session, ada, task.id, title="Echoed", expected_updated_at=echoed)


def test_a_tag_change_moves_updated_at_and_writes_a_snapshot(session, ada, bot):
    task = create_task(session, ada, "Call Chad", tags="deal:chad")
    tagged = update_task(session, bot, task.id, add="urgent")
    assert tagged.updated_at > task.updated_at and tagged.updated_by == bot.id
    entries = task_history(session, ada, task.id)
    assert len(entries) == 2 and entries[-1].state["tags"] == "deal:chad urgent"


def test_every_returned_datetime_is_in_utc(session, ada):
    task = create_task(session, ada, "Call Chad", due_at=datetime(2026, 7, 1, 17))
    child = create_task(session, ada, "Step", parent_id=task.id)
    views = [get_task(session, ada, task.id), *list_tasks(session, ada), *task_tree(session, ada)]
    for view in views:
        for moment in (view.created_at, view.updated_at, view.due_at):
            if moment is not None:
                assert moment.utcoffset() == timedelta(0)
    assert child.created_at.utcoffset() == timedelta(0)
    assert all(
        e.created_at.utcoffset() == timedelta(0) for e in task_history(session, ada, task.id)
    )


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


def test_an_agent_cannot_reach_another_accounts_tasks(session, ada, bot):
    _bea_id, bea_secret = second_account(session)
    bea = act_as(session, bea_secret)
    beas = create_task(session, bea, "Bea's task")
    calls = (
        lambda: get_task(session, bot, beas.id),
        lambda: update_task(session, bot, beas.id, title="Taken"),
        lambda: drop_task(session, bot, beas.id),
        lambda: delete_task(session, bot, beas.id),
        lambda: create_task(session, bot, "Nested", parent_id=beas.id),
    )
    for call in calls:
        with pytest.raises(NotFound):
            call()
    assert task_tree(session, bot) == []


def test_a_filtered_tree_keeps_each_matchs_ancestors(session, ada):
    root = create_task(session, ada, "Deal", status=COMPLETED)
    middle = create_task(session, ada, "Legal", parent_id=root.id, status=COMPLETED)
    leaf = create_task(session, ada, "Sign", parent_id=middle.id, tags="deal:chad")
    other = create_task(session, ada, "Unrelated", tags="home")
    create_task(session, ada, "Closed branch", parent_id=root.id, status=COMPLETED)

    def shape(tasks) -> list[tuple[int, bool]]:
        return [(task.id, task.matched) for task in tasks]

    by_tag = task_tree(session, ada, tags="deal:chad")
    assert shape(by_tag) == [(root.id, False), (middle.id, False), (leaf.id, True)]
    opened = task_tree(session, ada, open_only=True)
    assert shape(opened) == [
        (root.id, False),
        (middle.id, False),
        (leaf.id, True),
        (other.id, True),
    ]
    # A subtree read keeps ancestors only down from the parent asked for.
    assert shape(task_tree(session, ada, middle.id, open_only=True)) == [(leaf.id, True)]
    assert all(task.matched for task in task_tree(session, ada))
    assert all(task.matched for task in list_tasks(session, ada, tags="home"))
