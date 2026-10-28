"""The duplicate confirmation the API and MCP share."""

import pytest

from barrelvault.account import add_agent
from barrelvault.errors import Conflict, Duplicate, Invalid
from barrelvault.models import User
from barrelvault.surface import DuplicateWarnings
from barrelvault.tasks import create_task, list_tasks
from tests.support import act_as, make_account

CONFIRM_FIRST = "confirm a duplicate only after its warning"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def ada(session) -> User:
    _human, secret = make_account(session)
    return act_as(session, secret)


def _refused_first(warnings: DuplicateWarnings, session, actor: User, title: str) -> None:
    with pytest.raises(Conflict) as raised:
        warnings.create(session, actor, title, allow_duplicate=True)
    assert raised.value.message == CONFIRM_FIRST


def test_a_confirmation_needs_a_warning_and_each_warning_confirms_one(session, ada):
    warnings = DuplicateWarnings()
    create_task(session, ada, "Pay rent")
    _refused_first(warnings, session, ada, "Pay rent")
    with pytest.raises(Duplicate):
        warnings.create(session, ada, "pay  RENT")
    second = warnings.create(session, ada, "Pay rent", allow_duplicate=True)
    assert second.title == "Pay rent"
    _refused_first(warnings, session, ada, "Pay rent")
    assert len(list_tasks(session, ada)) == 2


def test_a_create_that_is_not_a_duplicate_goes_straight_through(session, ada):
    warnings = DuplicateWarnings()
    assert warnings.create(session, ada, "Pay rent", tags="home").tags == ["home"]
    with pytest.raises(Invalid):
        warnings.create(session, ada, " ")
    _refused_first(warnings, session, ada, "Pay rent")


def test_another_acting_user_cannot_confirm_a_warning(session, ada):
    agent = add_agent(session, ada, "Bot")
    bot = session.get(User, agent.user_id)
    warnings = DuplicateWarnings()
    create_task(session, ada, "Pay rent")
    with pytest.raises(Duplicate):
        warnings.create(session, ada, "Pay rent")
    _refused_first(warnings, session, bot, "Pay rent")
    assert warnings.create(session, ada, "Pay rent", allow_duplicate=True)


def test_the_record_drops_warnings_past_the_window_and_past_its_size(session, ada):
    clock = Clock()
    warnings = DuplicateWarnings(window_seconds=120, max_warnings=2, clock=clock)
    for title in ("One", "Two", "Three"):
        create_task(session, ada, title)
    with pytest.raises(Duplicate):
        warnings.create(session, ada, "One")
    clock.now += 121
    _refused_first(warnings, session, ada, "One")
    for title in ("One", "Two", "Three"):
        with pytest.raises(Duplicate):
            warnings.create(session, ada, title)
    _refused_first(warnings, session, ada, "One")
    assert warnings.create(session, ada, "Two", allow_duplicate=True)
    assert warnings.create(session, ada, "Three", allow_duplicate=True)


def test_a_warning_and_its_confirmation_fold_case_the_same_way(session, ada):
    warnings = DuplicateWarnings()
    create_task(session, ada, "Pay rent")
    with pytest.raises(Duplicate):
        warnings.create(session, ada, "PAY RENT")
    assert warnings.create(session, ada, "pay rent", allow_duplicate=True)


def test_a_confirmation_that_fails_keeps_its_warning(session, ada):
    warnings = DuplicateWarnings()
    create_task(session, ada, "Pay rent")
    with pytest.raises(Duplicate):
        warnings.create(session, ada, "Pay rent")
    with pytest.raises(Invalid):
        warnings.create(session, ada, "Pay rent", allow_duplicate=True, estimate_minutes=-1)
    assert warnings.create(session, ada, "Pay rent", allow_duplicate=True)


def test_the_default_window_is_two_minutes(session, ada):
    clock = Clock()
    warnings = DuplicateWarnings(clock=clock)
    for title in ("Kept", "Lapsed"):
        create_task(session, ada, title)
        with pytest.raises(Duplicate):
            warnings.create(session, ada, title)
    clock.now += 119
    assert warnings.create(session, ada, "Kept", allow_duplicate=True)
    clock.now += 2
    _refused_first(warnings, session, ada, "Lapsed")
