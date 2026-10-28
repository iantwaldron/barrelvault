import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from barrelvault import auth
from barrelvault.account import (
    add_agent,
    create_account,
    delete_agent,
    issue_key,
    reassign_agent,
    retire_agent,
    roll_key,
)
from barrelvault.auth import authenticate, exchange_key
from barrelvault.errors import Conflict, Forbidden, Invalid, NotFound, ServiceError
from barrelvault.guards import SETUP_WRITE
from barrelvault.models import Agent, Human, Token, User
from tests.support import (
    KEY_LENGTH,
    SESSION_LENGTH,
    act_as,
    live_agent,
    make_account,
    second_account,
    update_agent,
)


def test_create_account_stores_one_human(session):
    human = create_account(session, "Ada", "ada@example.com", "password-1")
    assert human.email == "ada@example.com"
    assert human.password_hash != "password-1"
    assert human.password_hash.startswith("$argon2")
    assert auth._passwords.verify("password-1", human.password_hash)
    user = session.get(User, human.user_id)
    assert user is not None
    assert user.name == "Ada"
    with pytest.raises(ServiceError):
        create_account(session, "Bea", "bea@example.com", "password-2")


def test_create_account_refuses_a_blank_field(session):
    with pytest.raises(ServiceError):
        create_account(session, " ", "ada@example.com", "password-1")
    with pytest.raises(ServiceError):
        create_account(session, "Ada", " ", "password-1")
    with pytest.raises(ServiceError):
        create_account(session, "Ada", "ada@example.com", " ")
    assert session.exec(select(Human)).all() == []


def test_a_logged_in_session_cannot_create_a_human(session):
    human, secret = make_account(session)
    _agent, issued = live_agent(session, secret)
    agent_secret = exchange_key(session, issued.key, session_length=SESSION_LENGTH)
    session.commit()
    for actor in (secret, agent_secret):
        act_as(session, actor)
        user = User(name="Bea")
        session.add(user)
        session.add(Human(user_id=9999, email="bea@example.com", password_hash="hash"))
        with pytest.raises(ServiceError):
            session.flush()
        session.rollback()
    assert session.exec(select(Human)).one().user_id == human.user_id


def test_only_setup_adds_a_human(session):
    create_account(session, "Ada", "ada@example.com", "password-1")
    session.commit()
    plain = Session(session.get_bind())
    user = User(name="Bea")
    plain.add(user)
    plain.flush()
    plain.add(Human(user_id=user.id, email="bea@example.com", password_hash="hash"))
    with pytest.raises(ServiceError):
        plain.flush()
    plain.rollback()
    plain.close()
    assert len(session.exec(select(Human)).all()) == 1
    assert SETUP_WRITE not in session.info


def test_add_stores_an_empty_key(session):
    _human, secret = make_account(session)
    agent = add_agent(session, act_as(session, secret), "Bot")
    assert agent.owner_id == _human.user_id
    assert agent.key_hash is None
    assert agent.key_expires_at is None
    assert agent.retired_at is None
    with pytest.raises(ServiceError):
        add_agent(session, act_as(session, secret), "Bot")
    with pytest.raises(ServiceError):
        add_agent(session, act_as(session, secret), "Ada")


def test_an_agent_name_is_unique_only_in_its_account(session):
    _human, secret = make_account(session)
    _bea_id, bea_secret = second_account(session)
    ada = act_as(session, secret)
    add_agent(session, ada, "Bot")
    for taken in ("bot", "BOT", "ada"):
        with pytest.raises(Conflict):
            add_agent(session, ada, taken)
    bea = act_as(session, bea_secret)
    assert add_agent(session, bea, "Bot").owner_id == bea.id
    assert add_agent(session, bea, "Ada").owner_id == bea.id


def test_issue_and_roll_set_the_key_expiry(session):
    _human, secret = make_account(session)
    agent = add_agent(session, act_as(session, secret), "Bot")
    before = datetime.now(UTC)
    issued = issue_key(session, act_as(session, secret), agent.user_id, key_length=100)
    after = datetime.now(UTC)
    session.refresh(agent)
    assert len(bytes.fromhex(issued.key)) == 32
    assert agent.key_hash != issued.key
    assert issued.key_expires_at == agent.key_expires_at
    assert before + timedelta(seconds=100) <= agent.key_expires_at
    assert agent.key_expires_at <= after + timedelta(seconds=100)
    with pytest.raises(ServiceError):
        issue_key(session, act_as(session, secret), agent.user_id, key_length=KEY_LENGTH)
    rolled = roll_key(session, act_as(session, secret), agent.user_id, key_length=KEY_LENGTH)
    session.refresh(agent)
    assert rolled.key != issued.key
    assert agent.key_hash != issued.key
    with pytest.raises(ServiceError):
        exchange_key(session, issued.key, session_length=SESSION_LENGTH)
    assert exchange_key(session, rolled.key, session_length=SESSION_LENGTH)


def test_issue_and_roll_refuse_an_empty_or_retired_agent(session):
    _human, secret = make_account(session)
    agent = add_agent(session, act_as(session, secret), "Bot")
    with pytest.raises(ServiceError):
        roll_key(session, act_as(session, secret), agent.user_id, key_length=KEY_LENGTH)
    issue_key(session, act_as(session, secret), agent.user_id, key_length=KEY_LENGTH)
    retire_agent(session, act_as(session, secret), agent.user_id)
    with pytest.raises(ServiceError):
        issue_key(session, act_as(session, secret), agent.user_id, key_length=KEY_LENGTH)
    with pytest.raises(ServiceError):
        roll_key(session, act_as(session, secret), agent.user_id, key_length=KEY_LENGTH)
    session.refresh(agent)
    assert agent.key_hash is None
    assert agent.key_expires_at is None


def test_retire_is_the_only_way_to_set_retired_at(session):
    _human, secret = make_account(session)
    agent, issued = live_agent(session, secret)
    agent_secret = exchange_key(session, issued.key, session_length=SESSION_LENGTH)
    session.commit()
    agent.retired_at = datetime.now(UTC)
    with pytest.raises(ServiceError):
        session.flush()
    session.rollback()
    assert session.get(Agent, agent.user_id).retired_at is None
    retire_agent(session, act_as(session, secret), agent.user_id)
    session.commit()
    retired = session.get(Agent, agent.user_id)
    retired.retired_at = None
    with pytest.raises(ServiceError):
        session.flush()
    session.rollback()
    assert session.get(Agent, agent.user_id).retired_at is not None
    with pytest.raises(ServiceError):
        authenticate(session, agent_secret)


def test_roll_replaces_an_expired_key(session):
    _human, secret = make_account(session)
    agent, issued = live_agent(session, secret)
    update_agent(
        session,
        agent.user_id,
        key_expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
    )
    with pytest.raises(ServiceError):
        exchange_key(session, issued.key, session_length=SESSION_LENGTH)
    rolled = roll_key(session, act_as(session, secret), agent.user_id, key_length=KEY_LENGTH)
    assert exchange_key(session, rolled.key, session_length=SESSION_LENGTH)


def test_key_edits_outside_issue_roll_and_retire_are_refused(session):
    _human, secret = make_account(session)
    agent, _issued = live_agent(session, secret)
    session.commit()
    session.refresh(agent)
    original_hash = agent.key_hash
    original_expiry = agent.key_expires_at
    original_owner = agent.owner_id
    agent.key_expires_at = original_expiry + timedelta(seconds=1)
    with pytest.raises(ServiceError):
        session.flush()
    agent.key_hash = None
    with pytest.raises(ServiceError):
        session.flush()
    agent.key_expires_at = None
    with pytest.raises(ServiceError):
        session.flush()
    agent.owner_id = agent.user_id
    with pytest.raises(ServiceError):
        session.flush()
    session.rollback()
    kept = session.get(Agent, agent.user_id)
    assert kept is not None
    assert kept.key_hash == original_hash
    assert kept.key_expires_at == original_expiry
    assert kept.owner_id == original_owner


def test_a_session_that_never_called_the_service_is_guarded(session):
    _human, secret = make_account(session)
    agent, _issued = live_agent(session, secret)
    session.commit()
    plain = Session(session.get_bind())
    stored = plain.get(Agent, agent.user_id)
    stored.key_hash = stored.key_hash
    plain.commit()
    stored.key_expires_at = datetime.now(UTC) + timedelta(days=9999)
    with pytest.raises(ServiceError):
        plain.commit()
    plain.rollback()
    stored = plain.get(Agent, agent.user_id)
    stored.owner_id = agent.user_id
    with pytest.raises(IntegrityError):
        plain.commit()
    plain.rollback()
    token = plain.exec(select(Token)).first()
    token.expires_at = token.expires_at + timedelta(days=1)
    with pytest.raises(IntegrityError):
        plain.commit()
    plain.close()


def test_a_bulk_update_cannot_skip_the_schema_rules(session):
    _human, secret = make_account(session)
    agent, _issued = live_agent(session, secret)
    session.commit()
    updates = (
        {"owner_id": agent.user_id},
        {"key_expires_at": None},
        {"retired_at": datetime.now(UTC)},
    )
    for values in updates:
        with pytest.raises(IntegrityError):
            session.execute(update(Agent).where(Agent.user_id == agent.user_id).values(**values))
        session.rollback()
    stored = session.get(Agent, agent.user_id)
    assert stored.owner_id == _human.user_id
    assert stored.key_expires_at is not None
    assert stored.retired_at is None


def test_owner_must_be_the_human(session):
    human, secret = make_account(session)
    agent = add_agent(session, act_as(session, secret), "Bot")
    bea_id, _bea_secret = second_account(session)
    session.commit()
    for owner_id in (agent.user_id, human.user_id + 100):
        user = User(name=f"Bot {owner_id}")
        session.add(user)
        session.flush()
        session.add(Agent(user_id=user.id, owner_id=owner_id))
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()
    agent = session.get(Agent, agent.user_id)
    agent.owner_id = bea_id
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()
    assert session.get(Agent, agent.user_id).owner_id == human.user_id


def test_an_unauthenticated_session_needs_a_human_owner(session):
    create_account(session, "Ada", "ada@example.com", "password-1")
    user = User(name="Bot")
    session.add(user)
    session.flush()
    session.add(Agent(user_id=user.id, owner_id=user.id))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()
    bea_id, _bea_secret = second_account(session)
    bot = User(name="Bea bot")
    session.add(bot)
    session.flush()
    session.add(Agent(user_id=bot.id, owner_id=bea_id))
    session.flush()
    assert session.get(Agent, bot.id).owner_id == bea_id


def test_profile_changes_are_refused(session):
    human, secret = make_account(session)
    agent = add_agent(session, act_as(session, secret), "Bot")
    session.commit()

    def refused(error: type[Exception], *writes) -> None:
        for write in writes:
            write()
        with pytest.raises(error):
            session.flush()
        session.rollback()

    def human_row(user_id: int) -> Human:
        return Human(user_id=user_id, email="bot@example.com", password_hash="hash")

    # A human user cannot become an agent, by replacement or beside its profile.
    refused(
        IntegrityError,
        lambda: session.delete(session.get(Human, human.user_id)),
        lambda: session.add(Agent(user_id=human.user_id, owner_id=human.user_id)),
    )
    refused(
        IntegrityError, lambda: session.add(Agent(user_id=human.user_id, owner_id=human.user_id))
    )
    # An agent user cannot become a human. Only setup adds a Human through the ORM.
    refused(
        ServiceError,
        lambda: session.delete(session.get(Agent, agent.user_id)),
        lambda: session.add(human_row(agent.user_id)),
    )
    refused(ServiceError, lambda: session.add(human_row(agent.user_id)))
    # The schema refuses it too, for a write that skips the ORM.
    with pytest.raises(IntegrityError):
        session.connection().execute(
            text(
                "INSERT INTO human (user_id, email, password_hash) "
                "VALUES (:user_id, 'bot@example.com', 'hash')"
            ),
            {"user_id": agent.user_id},
        )
    session.rollback()
    assert session.get(Human, human.user_id) is not None
    assert session.get(Agent, human.user_id) is None
    assert session.get(Agent, agent.user_id) is not None
    assert session.get(Human, agent.user_id) is None


def test_the_service_acts_as_a_user_without_a_session(session):
    human = create_account(session, "Ada", "ada@example.com", "password-1")
    ada = session.get(User, human.user_id)
    agent = add_agent(session, ada, "Bot")
    issued = issue_key(session, ada, agent.user_id, key_length=KEY_LENGTH)
    assert agent.owner_id == human.user_id
    assert session.exec(select(Token)).all() == []
    assert exchange_key(session, issued.key, session_length=SESSION_LENGTH)


def test_an_acting_agent_cannot_change_agents(session):
    human, secret = make_account(session)
    agent, issued = live_agent(session, secret)
    agent_secret = exchange_key(session, issued.key, session_length=SESSION_LENGTH)
    calls = (
        lambda: add_agent(session, act_as(session, agent_secret), "Other"),
        lambda: issue_key(
            session, act_as(session, agent_secret), agent.user_id, key_length=KEY_LENGTH
        ),
        lambda: roll_key(
            session, act_as(session, agent_secret), agent.user_id, key_length=KEY_LENGTH
        ),
        lambda: retire_agent(session, act_as(session, agent_secret), agent.user_id),
        lambda: reassign_agent(session, act_as(session, agent_secret), agent.user_id),
        lambda: delete_agent(session, act_as(session, agent_secret), agent.user_id),
    )
    for call in calls:
        with pytest.raises(ServiceError):
            call()
    assert session.get(User, human.user_id) is not None
    assert session.get(Agent, agent.user_id) is not None
    assert session.get(Human, human.user_id) is not None


def test_a_human_cannot_change_another_accounts_agent(session):
    _human, secret = make_account(session)
    bea_id, bea_secret = second_account(session)
    bea_bot, bea_key = live_agent(session, bea_secret, "Bea bot")
    bea_bare = add_agent(session, act_as(session, bea_secret), "Bea bare")
    bea_bot_secret = exchange_key(session, bea_key.key, session_length=SESSION_LENGTH)
    session.commit()
    calls = (
        lambda: issue_key(
            session, act_as(session, secret), bea_bare.user_id, key_length=KEY_LENGTH
        ),
        lambda: roll_key(session, act_as(session, secret), bea_bot.user_id, key_length=KEY_LENGTH),
        lambda: retire_agent(session, act_as(session, secret), bea_bot.user_id),
        lambda: delete_agent(session, act_as(session, secret), bea_bare.user_id),
        lambda: reassign_agent(session, act_as(session, secret), bea_bot.user_id),
    )
    for call in calls:
        with pytest.raises(ServiceError):
            call()
    session.expire_all()
    bot = session.get(Agent, bea_bot.user_id)
    bare = session.get(Agent, bea_bare.user_id)
    assert bot.owner_id == bea_id
    assert bot.key_hash == hashlib.sha256(bea_key.key.encode()).hexdigest()
    assert bot.retired_at is None
    assert bare is not None
    assert bare.key_hash is None
    assert authenticate(session, bea_bot_secret).id == bea_bot.user_id


def test_roll_and_retire_delete_only_the_agent_session(session):
    human, secret = make_account(session)
    agent, issued = live_agent(session, secret)
    agent_secret = exchange_key(session, issued.key, session_length=SESSION_LENGTH)
    rolled = roll_key(session, act_as(session, secret), agent.user_id, key_length=KEY_LENGTH)
    assert authenticate(session, secret).id == human.user_id
    with pytest.raises(ServiceError):
        authenticate(session, agent_secret)
    assert session.exec(select(Token).where(Token.user_id == agent.user_id)).first() is None
    agent_secret = exchange_key(session, rolled.key, session_length=SESSION_LENGTH)
    assert session.exec(select(Token).where(Token.user_id == agent.user_id)).one()
    retire_agent(session, act_as(session, secret), agent.user_id)
    with pytest.raises(ServiceError):
        authenticate(session, agent_secret)
    with pytest.raises(ServiceError):
        exchange_key(session, rolled.key, session_length=SESSION_LENGTH)
    assert authenticate(session, secret).id == human.user_id
    session.refresh(agent)
    assert agent.retired_at is not None
    assert agent.key_hash is None
    assert agent.key_expires_at is None
    assert session.get(User, agent.user_id) is not None


def test_roll_with_no_session_leaves_none(session):
    _human, secret = make_account(session)
    agent, issued = live_agent(session, secret)
    roll_key(session, act_as(session, secret), agent.user_id, key_length=KEY_LENGTH)
    assert session.exec(select(Token).where(Token.user_id == agent.user_id)).first() is None


def test_retire_rolls_back_with_the_transaction(session):
    _human, secret = make_account(session)
    agent, issued = live_agent(session, secret)
    agent_secret = exchange_key(session, issued.key, session_length=SESSION_LENGTH)
    session.commit()
    retire_agent(session, act_as(session, secret), agent.user_id)
    session.rollback()
    kept = session.get(Agent, agent.user_id)
    assert kept is not None
    assert kept.retired_at is None
    assert kept.key_hash is not None
    assert authenticate(session, agent_secret).id == agent.user_id


def test_delete_removes_an_agent_with_nothing_attached(session):
    human, secret = make_account(session)
    agent, issued = live_agent(session, secret)
    exchange_key(session, issued.key, session_length=SESSION_LENGTH)
    assert delete_agent(session, act_as(session, secret), agent.user_id) is True
    assert session.get(Agent, agent.user_id) is None
    assert session.get(User, agent.user_id) is None
    assert session.exec(select(Token).where(Token.user_id == agent.user_id)).first() is None
    assert session.get(Human, human.user_id) is not None


def test_delete_does_nothing_when_data_is_attached(session):
    human, secret = make_account(session)
    agent = add_agent(session, act_as(session, secret), "Bot")
    _attach(session, agent.user_id, human.user_id)
    assert delete_agent(session, act_as(session, secret), agent.user_id) is False
    assert session.get(Agent, agent.user_id) is not None
    assert session.get(User, agent.user_id) is not None
    task = session.connection().execute(text("SELECT created_by, owned_by FROM task")).one()
    assert task == (agent.user_id, human.user_id)


def test_reassign_moves_references_and_keeps_owned_by(session):
    human, secret = make_account(session)
    agent, issued = live_agent(session, secret)
    exchange_key(session, issued.key, session_length=SESSION_LENGTH)
    _attach(session, agent.user_id, human.user_id)
    reassign_agent(session, act_as(session, secret), agent.user_id)
    assert session.get(Agent, agent.user_id) is None
    assert session.get(User, agent.user_id) is None
    assert session.get(Human, human.user_id) is not None
    task = (
        session.connection()
        .execute(text("SELECT created_by, updated_by, owned_by FROM task"))
        .one()
    )
    block = session.connection().execute(text("SELECT created_by, released_by FROM block")).one()
    history = session.connection().execute(text("SELECT user_id FROM task_history")).one()
    assert task == (human.user_id, human.user_id, human.user_id)
    assert block == (human.user_id, human.user_id)
    assert history == (human.user_id,)


def test_create_account_refuses_once_any_user_exists(session):
    session.add(User(name="Bot"))
    session.flush()
    with pytest.raises(ServiceError):
        create_account(session, "Ada", "ada@example.com", "password-1")
    assert session.exec(select(Human)).all() == []


def test_each_refusal_names_its_kind(session):
    _human, secret = make_account(session)
    ada = act_as(session, secret)
    bea_id, bea_secret = second_account(session)
    bea_bot = add_agent(session, act_as(session, bea_secret), "Bea bot")
    keyed, issued = live_agent(session, secret, "Keyed")
    bare = add_agent(session, ada, "Bare")
    retired = add_agent(session, ada, "Retired")
    retire_agent(session, ada, retired.user_id)
    bot = session.get(User, keyed.user_id)
    cases = (
        (Invalid, "name is required", lambda: add_agent(session, ada, " ")),
        (Conflict, "name is taken", lambda: add_agent(session, ada, "Keyed")),
        (Forbidden, "only a person changes agents", lambda: add_agent(session, bot, "Other")),
        (NotFound, "no such agent", lambda: retire_agent(session, ada, 9999)),
        (NotFound, "no such agent", lambda: retire_agent(session, ada, bea_bot.user_id)),
        (
            Conflict,
            "agent already has a key",
            lambda: issue_key(session, ada, keyed.user_id, key_length=KEY_LENGTH),
        ),
        (
            Conflict,
            "agent has no key",
            lambda: roll_key(session, ada, bare.user_id, key_length=KEY_LENGTH),
        ),
        (
            Conflict,
            "agent is retired",
            lambda: issue_key(session, ada, retired.user_id, key_length=KEY_LENGTH),
        ),
        (
            Conflict,
            "agent is retired",
            lambda: roll_key(session, ada, retired.user_id, key_length=KEY_LENGTH),
        ),
        (
            Conflict,
            "setup has already run",
            lambda: create_account(session, "Eve", "e@example.com", "pw"),
        ),
        (Invalid, "email is required", lambda: create_account(session, "Eve", " ", "pw")),
    )
    for kind, message, call in cases:
        with pytest.raises(kind) as raised:
            call()
        assert raised.value.message == message
    assert bea_id


def test_a_missing_agent_is_refused(session):
    _human, secret = make_account(session)
    with pytest.raises(ServiceError):
        issue_key(session, act_as(session, secret), 9999, key_length=KEY_LENGTH)


def test_reassign_with_nothing_attached_removes_the_agent(session):
    human, secret = make_account(session)
    agent = add_agent(session, act_as(session, secret), "Bot")
    reassign_agent(session, act_as(session, secret), agent.user_id)
    assert session.get(User, agent.user_id) is None
    assert session.get(Human, human.user_id) is not None


def _attach(session: Session, agent_user_id: int, human_user_id: int) -> None:
    connection = session.connection()
    connection.execute(
        text(
            "CREATE TABLE task ("
            "id INTEGER PRIMARY KEY, created_by INTEGER, updated_by INTEGER, owned_by INTEGER)"
        )
    )
    connection.execute(
        text("CREATE TABLE block (id INTEGER PRIMARY KEY, created_by INTEGER, released_by INTEGER)")
    )
    connection.execute(text("CREATE TABLE task_history (id INTEGER PRIMARY KEY, user_id INTEGER)"))
    connection.execute(
        text(
            "INSERT INTO task (created_by, updated_by, owned_by) "
            "VALUES (:agent_user_id, :agent_user_id, :human_user_id)"
        ),
        {"agent_user_id": agent_user_id, "human_user_id": human_user_id},
    )
    connection.execute(
        text("INSERT INTO block (created_by, released_by) VALUES (:agent_user_id, :agent_user_id)"),
        {"agent_user_id": agent_user_id},
    )
    connection.execute(
        text("INSERT INTO task_history (user_id) VALUES (:agent_user_id)"),
        {"agent_user_id": agent_user_id},
    )
