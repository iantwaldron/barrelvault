"""The task, block, and history tables: their constraints, triggers, and recorder."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from barrelvault.models import Agent, Block, Task, TaskHistory, User
from barrelvault.status import COMPLETED, DROPPED, IN_PROGRESS, TODO

NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)


def _person(session: Session, name: str, email: str) -> int:
    connection = session.connection()
    user_id = connection.execute(
        text("INSERT INTO user (name) VALUES (:name) RETURNING id"), {"name": name}
    ).scalar_one()
    connection.execute(
        text(
            "INSERT INTO human (user_id, email, password_hash, timezone) "
            "VALUES (:user_id, :email, 'hash', 'UTC')"
        ),
        {"user_id": user_id, "email": email},
    )
    return user_id


def _agent(session: Session, owner_id: int, name: str) -> int:
    user = User(name=name)
    session.add(user)
    session.flush()
    session.add(Agent(user_id=user.id, owner_id=owner_id))
    session.flush()
    return user.id


def _task(session: Session, owner_id: int, actor_id: int | None = None, **fields) -> Task:
    actor = actor_id or owner_id
    task = Task(
        title=fields.pop("title", "Call Chad"),
        created_by=actor,
        created_at=NOW,
        updated_by=actor,
        updated_at=NOW,
        owned_by=owner_id,
        **fields,
    )
    session.add(task)
    session.flush()
    return task


def _history(session: Session, task_id: int) -> list[TaskHistory]:
    query = select(TaskHistory).where(TaskHistory.task_id == task_id)
    return list(session.exec(query.order_by(TaskHistory.created_at, TaskHistory.id)))


def _refused(session: Session, statement: str, **values) -> None:
    with pytest.raises(IntegrityError):
        session.connection().execute(text(statement), values)
    session.rollback()


@pytest.fixture
def ada(session):
    ada_id = _person(session, "Ada", "ada@example.com")
    session.commit()
    return ada_id


def test_a_create_records_the_first_snapshot(session, ada):
    task = _task(session, ada, due_at=NOW + timedelta(days=1), estimate_minutes=30)
    [row] = _history(session, task.id)
    assert row.user_id == ada
    assert row.state == {
        "parent_id": None,
        "title": "Call Chad",
        "notes": "",
        "tags": "",
        "status": TODO,
        "estimate_minutes": 30,
        "due_at": "2026-01-02T12:00:00+00:00",
    }


def test_an_update_records_a_snapshot_only_when_a_recorded_field_changes(session, ada):
    task = _task(session, ada)
    bot = _agent(session, ada, "Bot")
    task.title = "Call Chad today"
    task.updated_by = bot
    session.flush()
    task.updated_at = NOW + timedelta(minutes=5)
    session.flush()
    task.title = "Call Chad today"
    session.flush()
    rows = _history(session, task.id)
    assert [row.state["title"] for row in rows] == ["Call Chad", "Call Chad today"]
    assert rows[1].user_id == bot


def test_a_rollback_drops_the_task_change_and_its_snapshot(session, ada):
    task = _task(session, ada)
    session.commit()
    task.title = "Changed"
    session.flush()
    session.rollback()
    assert session.get(Task, task.id).title == "Call Chad"
    assert len(_history(session, task.id)) == 1


def test_a_delete_takes_the_subtree_with_its_blocks_and_history(session, ada):
    parent = _task(session, ada)
    child = _task(session, ada, parent_id=parent.id)
    other = _task(session, ada)
    session.add(Block(task_id=child.id, reason="Waiting on Chad", created_by=ada, created_at=NOW))
    session.commit()
    session.delete(parent)
    session.commit()
    assert session.exec(select(Task)).all() == [other]
    assert session.exec(select(Block)).all() == []
    assert {row.task_id for row in session.exec(select(TaskHistory))} == {other.id}


def test_a_task_is_owned_by_a_human_and_keeps_its_owner(session, ada):
    bot = _agent(session, ada, "Bot")
    session.commit()
    with pytest.raises(IntegrityError):
        _task(session, bot)
    session.rollback()
    task = _task(session, ada)
    session.commit()
    for column, value in (("owned_by", bot), ("created_at", "2000-01-01 00:00:00")):
        _refused(
            session, f"UPDATE task SET {column} = :value WHERE id = :id", value=value, id=task.id
        )


def test_created_by_moves_only_from_an_agent_to_its_owner(session, ada):
    bea = _person(session, "Bea", "bea@example.com")
    bot = _agent(session, ada, "Bot")
    task = _task(session, ada, actor_id=bot)
    mine = _task(session, ada)
    session.commit()
    _refused(session, "UPDATE task SET created_by = :to WHERE id = :id", to=bea, id=task.id)
    _refused(session, "UPDATE task SET created_by = :to WHERE id = :id", to=bot, id=mine.id)
    session.connection().execute(
        text("UPDATE task SET created_by = :to WHERE id = :id"), {"to": ada, "id": task.id}
    )
    session.commit()
    assert session.get(Task, task.id).created_by == ada


def test_a_parent_is_in_the_same_account(session, ada):
    bea = _person(session, "Bea", "bea@example.com")
    beas = _task(session, bea)
    mine = _task(session, ada)
    session.commit()
    with pytest.raises(IntegrityError):
        _task(session, ada, parent_id=beas.id)
    session.rollback()
    _refused(session, "UPDATE task SET parent_id = :p WHERE id = :id", p=beas.id, id=mine.id)


def test_the_task_columns_are_checked(session, ada):
    _task(session, ada)
    session.commit()
    for column, value in (("status", 4), ("estimate_minutes", -1), ("estimate_minutes", 32768)):
        _refused(session, f"UPDATE task SET {column} = :value", value=value)
    _refused(session, "UPDATE task SET title = '   '")


def test_a_block_is_added_only_to_an_open_task(session, ada):
    for status in (COMPLETED, DROPPED):
        task = _task(session, ada, status=status)
        session.commit()
        with pytest.raises(IntegrityError):
            session.add(Block(task_id=task.id, reason="Waiting", created_by=ada, created_at=NOW))
            session.flush()
        session.rollback()


def test_a_blocked_task_keeps_its_status_in_either_direction(session, ada):
    task = _task(session, ada, status=IN_PROGRESS)
    block = Block(task_id=task.id, reason="Waiting on Chad", created_by=ada, created_at=NOW)
    session.add(block)
    session.commit()
    for status in (TODO, COMPLETED, DROPPED):
        _refused(session, "UPDATE task SET status = :s WHERE id = :id", s=status, id=task.id)
    session.connection().execute(
        text("UPDATE block SET released_by = :by, released_at = :at WHERE id = :id"),
        {"by": ada, "at": NOW.isoformat(), "id": block.id},
    )
    session.connection().execute(text("UPDATE task SET status = 2 WHERE id = :id"), {"id": task.id})
    session.commit()
    assert session.get(Task, task.id).status == COMPLETED


def test_a_release_is_final_and_a_block_stays_on_its_task(session, ada):
    task = _task(session, ada)
    other = _task(session, ada)
    block = Block(
        task_id=task.id,
        reason="Waiting",
        created_by=ada,
        created_at=NOW,
        released_by=ada,
        released_at=NOW,
    )
    session.add(block)
    session.commit()
    _refused(session, "UPDATE block SET released_at = NULL, released_by = NULL")
    _refused(session, "UPDATE block SET released_at = '2030-01-01 00:00:00'")
    _refused(session, "UPDATE block SET task_id = :t", t=other.id)
    _refused(session, "UPDATE block SET created_at = '2000-01-01 00:00:00'")


def test_the_block_columns_are_checked(session, ada):
    task = _task(session, ada)
    session.commit()
    statement = (
        "INSERT INTO block (task_id, reason, created_by, created_at, released_by, released_at) "
        "VALUES (:task, :reason, :by, '2026-01-01 00:00:00', :released_by, :released_at)"
    )
    for reason, released_by in (("  ", None), ("two\nlines", None), ("Waiting", ada)):
        _refused(
            session,
            statement,
            task=task.id,
            reason=reason,
            by=ada,
            released_by=released_by,
            released_at=None,
        )


def test_a_history_row_is_fixed_except_for_a_reassign(session, ada):
    task = _task(session, ada)
    session.commit()
    _refused(session, "UPDATE task_history SET state = '{}'")
    _refused(session, "UPDATE task_history SET created_at = '2000-01-01 00:00:00'")
    other = _task(session, ada)
    session.commit()
    _refused(
        session, "UPDATE task_history SET task_id = :t WHERE task_id = :id", t=other.id, id=task.id
    )


def test_open_for_stops_at_the_last_update_once_closed(session, ada):
    task = _task(session, ada)
    assert task.open_for(NOW + timedelta(hours=3)) == timedelta(hours=3)
    task.status = COMPLETED
    task.updated_at = NOW + timedelta(hours=1)
    assert task.open_for(NOW + timedelta(days=9)) == timedelta(hours=1)
    task.status = DROPPED
    assert task.open_for() == timedelta(hours=1)


def test_title_key_follows_the_title_and_stays_out_of_history(session, ada):
    task = _task(session, ada, title="Café  Rent")
    assert task.title_key == "café rent"
    task.title = "STRASSE"
    session.flush()
    assert task.title_key == "strasse"
    assert all("title_key" not in row.state for row in _history(session, task.id))


def test_a_block_or_history_row_goes_only_with_its_task(session, ada):
    task = _task(session, ada)
    session.add(Block(task_id=task.id, reason="Waiting", created_by=ada, created_at=NOW))
    session.commit()
    _refused(session, "DELETE FROM block")
    _refused(session, "DELETE FROM task_history")
    session.delete(session.get(Task, task.id))
    session.commit()
    assert session.exec(select(Block)).all() == []


def test_a_released_block_keeps_who_released_it(session, ada):
    bea = _person(session, "Bea", "bea@example.com")
    task = _task(session, ada)
    session.add(
        Block(
            task_id=task.id,
            reason="Waiting",
            created_by=ada,
            created_at=NOW,
            released_by=ada,
            released_at=NOW,
        )
    )
    session.commit()
    _refused(session, "UPDATE block SET released_by = :to", to=bea)


def test_blank_text_and_non_integer_numbers_are_refused_by_the_schema(session, ada):
    task = _task(session, ada)
    session.commit()
    tab, cr = "\t", "\r"
    _refused(session, "UPDATE task SET title = :t", t=f" {tab}{cr} ")
    _refused(session, "UPDATE task SET estimate_minutes = 5.5")
    _refused(session, "UPDATE task SET status = 1.5")
    _refused(
        session,
        "INSERT INTO block (task_id, reason, created_by, created_at) "
        "VALUES (:t, :r, :by, '2026-01-01 00:00:00')",
        t=task.id,
        r=f"carriage{cr}return",
        by=ada,
    )
