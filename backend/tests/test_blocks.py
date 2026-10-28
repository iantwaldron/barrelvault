"""The block service: open tasks only, release, edit, the account boundary, follow-ups."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from barrelvault.account import add_agent
from barrelvault.blocks import (
    add_block,
    edit_block_reason,
    list_blocks,
    release_block,
)
from barrelvault.errors import Conflict, Invalid, NotFound
from barrelvault.models import User
from barrelvault.status import COMPLETED, DROPPED, IN_PROGRESS
from barrelvault.tasks import create_task, get_task, task_history
from tests.support import act_as, make_account, second_account


@pytest.fixture
def ada(session) -> User:
    _human, secret = make_account(session)
    return act_as(session, secret)


@pytest.fixture
def bot(session, ada) -> User:
    agent = add_agent(session, ada, "Bot")
    return session.get(User, agent.user_id)


def test_a_block_marks_the_task_blocked_and_changed_without_a_snapshot(session, ada, bot):
    task = create_task(session, ada, "Call Chad")
    block = add_block(session, bot, task.id, "  Waiting on Chad's reply, asked Monday  ")
    assert block.reason == "Waiting on Chad's reply, asked Monday"
    assert (block.created_by, block.released_by, block.released_at) == (bot.id, None, None)
    blocked = get_task(session, ada, task.id)
    assert [open_block.released_at for open_block in list_blocks(session, ada, task.id)] == [None]
    assert blocked.updated_by == bot.id and blocked.updated_at > task.updated_at
    before_release = get_task(session, ada, task.id)
    released = release_block(session, ada, block.id)
    assert released.released_by == ada.id and released.released_at is not None
    after_release = get_task(session, ada, task.id)
    assert list_blocks(session, ada, task.id)[0].released_at is not None
    assert after_release.updated_by == ada.id
    assert after_release.updated_at > before_release.updated_at
    assert len(task_history(session, ada, task.id)) == 1


def test_every_block_change_moves_the_tasks_updated_at(session, ada, bot):
    task = create_task(session, ada, "Call Chad")
    block = add_block(session, ada, task.id, "Waiting on Chad")
    changes = (
        lambda: add_block(session, bot, task.id, "Waiting on Sam"),
        lambda: edit_block_reason(session, bot, block.id, "Waiting on Chad's reply"),
        lambda: release_block(session, bot, block.id),
    )
    for change in changes:
        before = get_task(session, ada, task.id).updated_at
        change()
        after = get_task(session, ada, task.id)
        assert after.updated_at > before and after.updated_by == bot.id
    assert len(task_history(session, ada, task.id)) == 1


def test_a_block_is_added_only_to_an_open_task(session, ada):
    for status in (COMPLETED, DROPPED):
        task = create_task(session, ada, f"Closed {status}", status=status)
        with pytest.raises(Conflict) as raised:
            add_block(session, ada, task.id, "Waiting")
        assert raised.value.message == "a block is added only to an open task"
    in_progress = create_task(session, ada, "Started", status=IN_PROGRESS)
    assert add_block(session, ada, in_progress.id, "Waiting").task_id == in_progress.id


def test_a_released_block_stays_released(session, ada):
    task = create_task(session, ada, "Call Chad")
    block = add_block(session, ada, task.id, "Waiting")
    release_block(session, ada, block.id)
    with pytest.raises(Conflict) as raised:
        release_block(session, ada, block.id)
    assert raised.value.message == "block is already released"


def test_a_reason_is_one_line_and_can_be_edited(session, ada, bot):
    task = create_task(session, ada, "Call Chad")
    for reason, message in ((" ", "reason is required"), ("two\nlines", "reason is one line")):
        with pytest.raises(Invalid) as raised:
            add_block(session, ada, task.id, reason)
        assert raised.value.message == message
    block = add_block(session, ada, task.id, "Waiting on Chad")
    before = get_task(session, ada, task.id).updated_at
    assert (
        edit_block_reason(session, ada, block.id, " Waiting on Chad ").reason == "Waiting on Chad"
    )
    assert get_task(session, ada, task.id).updated_at == before
    edited = edit_block_reason(session, bot, block.id, "Waiting on Sam, not Chad")
    assert edited.reason == "Waiting on Sam, not Chad"
    assert get_task(session, ada, task.id).updated_by == bot.id
    with pytest.raises(Invalid):
        edit_block_reason(session, ada, block.id, "a\rb")


def test_blocks_list_in_order_open_and_released(session, ada):
    task = create_task(session, ada, "Call Chad")
    first = add_block(session, ada, task.id, "First")
    second = add_block(session, ada, task.id, "Second")
    release_block(session, ada, first.id)
    listed = list_blocks(session, ada, task.id)
    assert [block.id for block in listed] == [first.id, second.id]
    assert listed[0].released_at is not None and listed[1].released_at is None


def test_another_accounts_blocks_are_not_found(session, ada):
    _bea_id, bea_secret = second_account(session)
    bea = act_as(session, bea_secret)
    beas_task = create_task(session, bea, "Bea's task")
    beas_block = add_block(session, bea, beas_task.id, "Waiting")
    calls = (
        lambda: add_block(session, ada, beas_task.id, "Taken"),
        lambda: release_block(session, ada, beas_block.id),
        lambda: edit_block_reason(session, ada, beas_block.id, "Taken"),
        lambda: list_blocks(session, ada, beas_task.id),
        lambda: release_block(session, ada, 9999),
    )
    for call in calls:
        with pytest.raises(NotFound):
            call()


def test_the_schema_refuses_a_status_change_on_a_blocked_task(session, ada):
    task = create_task(session, ada, "Call Chad")
    add_block(session, ada, task.id, "Waiting")
    session.commit()
    with pytest.raises(IntegrityError):
        session.connection().execute(
            text("UPDATE task SET status = 2 WHERE id = :id"), {"id": task.id}
        )


def test_an_agent_cannot_reach_another_accounts_blocks(session, ada, bot):
    _bea_id, bea_secret = second_account(session)
    bea = act_as(session, bea_secret)
    beas_task = create_task(session, bea, "Bea's task")
    beas_block = add_block(session, bea, beas_task.id, "Waiting")
    calls = (
        lambda: add_block(session, bot, beas_task.id, "Taken"),
        lambda: release_block(session, bot, beas_block.id),
        lambda: edit_block_reason(session, bot, beas_block.id, "Taken"),
        lambda: list_blocks(session, bot, beas_task.id),
    )
    for call in calls:
        with pytest.raises(NotFound):
            call()
