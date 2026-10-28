import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from barrelvault import auth
from barrelvault.auth import ServiceError, authenticate, exchange_key, login
from barrelvault.guards import SETUP_WRITE
from barrelvault.models import Agent, Human, Token, User
from barrelvault.account import (
    IssuedKey,
    add_agent,
    create_account,
    delete_agent,
    issue_key,
    reassign_agent,
    retire_agent,
    roll_key,
)
from barrelvault.settings import SettingsError


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _foreign_keys(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    SQLModel.metadata.create_all(engine)
    with Session(engine) as db_session:
        yield db_session


def _actor(session: Session, secret: str) -> User:
    """Authenticate the way the API does, then act as that user."""
    return authenticate(session, secret)


def _account(session: Session) -> tuple[Human, str]:
    human = create_account(session, "Ada", "ada@example.com", "password-1")
    secret = login(session, "ada@example.com", "password-1")
    return human, secret


def _second_account(session: Session) -> tuple[int, str]:
    """Insert a second human directly, the way the spec tells tests to."""
    connection = session.connection()
    user_id = connection.execute(
        text("INSERT INTO user (name) VALUES ('Bea') RETURNING id")
    ).scalar_one()
    connection.execute(
        text(
            "INSERT INTO human (user_id, email, password_hash) "
            "VALUES (:user_id, 'bea@example.com', :password_hash)"
        ),
        {"user_id": user_id, "password_hash": auth.hash_password("password-2")},
    )
    return user_id, login(session, "bea@example.com", "password-2")


def _live_agent(session: Session, secret: str, name: str = "Bot") -> tuple[Agent, IssuedKey]:
    agent = add_agent(session, _actor(session, secret), name)
    issued = issue_key(session, _actor(session, secret), agent.user_id)
    return agent, issued


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
    human, secret = _account(session)
    _agent, issued = _live_agent(session, secret)
    agent_secret = exchange_key(session, issued.key)
    session.commit()
    for actor in (secret, agent_secret):
        _actor(session, actor)
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
    _human, secret = _account(session)
    agent = add_agent(session, _actor(session, secret), "Bot")
    assert agent.owner_id == _human.user_id
    assert agent.key_hash is None
    assert agent.key_expires_at is None
    assert agent.retired_at is None
    with pytest.raises(ServiceError):
        add_agent(session, _actor(session, secret), "Bot")
    with pytest.raises(ServiceError):
        add_agent(session, _actor(session, secret), "Ada")


def test_issue_and_roll_set_the_key_expiry(session, monkeypatch):
    monkeypatch.setenv("BARRELVAULT_KEY_LENGTH", "100")
    _human, secret = _account(session)
    agent = add_agent(session, _actor(session, secret), "Bot")
    before = datetime.now(UTC)
    issued = issue_key(session, _actor(session, secret), agent.user_id)
    after = datetime.now(UTC)
    session.refresh(agent)
    assert len(bytes.fromhex(issued.key)) == 32
    assert agent.key_hash != issued.key
    assert issued.key_expires_at == agent.key_expires_at
    assert before + timedelta(seconds=100) <= agent.key_expires_at
    assert agent.key_expires_at <= after + timedelta(seconds=100)
    with pytest.raises(ServiceError):
        issue_key(session, _actor(session, secret), agent.user_id)
    rolled = roll_key(session, _actor(session, secret), agent.user_id)
    session.refresh(agent)
    assert rolled.key != issued.key
    assert agent.key_hash != issued.key
    with pytest.raises(ServiceError):
        exchange_key(session, issued.key)
    assert exchange_key(session, rolled.key)


def test_issue_uses_the_default_key_length(session, monkeypatch):
    monkeypatch.delenv("BARRELVAULT_KEY_LENGTH", raising=False)
    _human, secret = _account(session)
    agent = add_agent(session, _actor(session, secret), "Bot")
    before = datetime.now(UTC)
    issue_key(session, _actor(session, secret), agent.user_id)
    after = datetime.now(UTC)
    session.refresh(agent)
    assert before + timedelta(seconds=31536000) <= agent.key_expires_at
    assert agent.key_expires_at <= after + timedelta(seconds=31536000)


def test_invalid_key_length_leaves_the_agent(session, monkeypatch):
    _human, secret = _account(session)
    agent = add_agent(session, _actor(session, secret), "Bot")
    monkeypatch.setenv("BARRELVAULT_KEY_LENGTH", "nope")
    with pytest.raises(SettingsError):
        issue_key(session, _actor(session, secret), agent.user_id)
    session.refresh(agent)
    assert agent.key_hash is None
    assert agent.key_expires_at is None


def test_issue_and_roll_refuse_an_empty_or_retired_agent(session):
    _human, secret = _account(session)
    agent = add_agent(session, _actor(session, secret), "Bot")
    with pytest.raises(ServiceError):
        roll_key(session, _actor(session, secret), agent.user_id)
    issue_key(session, _actor(session, secret), agent.user_id)
    retire_agent(session, _actor(session, secret), agent.user_id)
    with pytest.raises(ServiceError):
        issue_key(session, _actor(session, secret), agent.user_id)
    with pytest.raises(ServiceError):
        roll_key(session, _actor(session, secret), agent.user_id)
    session.refresh(agent)
    assert agent.key_hash is None
    assert agent.key_expires_at is None


def test_retire_is_the_only_way_to_set_retired_at(session):
    _human, secret = _account(session)
    agent, issued = _live_agent(session, secret)
    agent_secret = exchange_key(session, issued.key)
    session.commit()
    agent.retired_at = datetime.now(UTC)
    with pytest.raises(ServiceError):
        session.flush()
    session.rollback()
    assert session.get(Agent, agent.user_id).retired_at is None
    retire_agent(session, _actor(session, secret), agent.user_id)
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
    _human, secret = _account(session)
    agent, issued = _live_agent(session, secret)
    _update_agent(
        session,
        agent.user_id,
        key_expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
    )
    with pytest.raises(ServiceError):
        exchange_key(session, issued.key)
    rolled = roll_key(session, _actor(session, secret), agent.user_id)
    assert exchange_key(session, rolled.key)


def test_key_edits_outside_issue_roll_and_retire_are_refused(session):
    _human, secret = _account(session)
    agent, _issued = _live_agent(session, secret)
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
    _human, secret = _account(session)
    agent, _issued = _live_agent(session, secret)
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
    with pytest.raises(ServiceError):
        plain.commit()
    plain.rollback()
    token = plain.exec(select(Token)).first()
    token.expires_at = token.expires_at + timedelta(days=1)
    with pytest.raises(ServiceError):
        plain.commit()
    plain.close()


def test_owner_must_be_the_human(session):
    human, secret = _account(session)
    agent = add_agent(session, _actor(session, secret), "Bot")
    bea_id, _bea_secret = _second_account(session)
    session.commit()
    for owner_id in (agent.user_id, human.user_id + 100):
        user = User(name=f"Bot {owner_id}")
        session.add(user)
        session.flush()
        session.add(Agent(user_id=user.id, owner_id=owner_id))
        with pytest.raises(ServiceError):
            session.flush()
        session.rollback()
    agent = session.get(Agent, agent.user_id)
    agent.owner_id = bea_id
    with pytest.raises(ServiceError):
        session.flush()
    session.rollback()
    assert session.get(Agent, agent.user_id).owner_id == human.user_id


def test_an_unauthenticated_session_needs_a_human_owner(session):
    create_account(session, "Ada", "ada@example.com", "password-1")
    user = User(name="Bot")
    session.add(user)
    session.flush()
    session.add(Agent(user_id=user.id, owner_id=user.id))
    with pytest.raises(ServiceError):
        session.flush()
    session.rollback()
    bea_id, _bea_secret = _second_account(session)
    bot = User(name="Bea bot")
    session.add(bot)
    session.flush()
    session.add(Agent(user_id=bot.id, owner_id=bea_id))
    session.flush()
    assert session.get(Agent, bot.id).owner_id == bea_id


def test_profile_changes_are_refused(session):
    human, secret = _account(session)
    agent = add_agent(session, _actor(session, secret), "Bot")
    session.commit()

    def refused(*writes) -> None:
        for write in writes:
            write()
        with pytest.raises(ServiceError):
            session.flush()
        session.rollback()

    stored_human = lambda: session.get(Human, human.user_id)
    stored_agent = lambda: session.get(Agent, agent.user_id)
    refused(
        lambda: session.delete(stored_human()),
        lambda: session.add(Agent(user_id=human.user_id, owner_id=human.user_id)),
    )
    refused(
        lambda: session.delete(stored_agent()),
        lambda: session.add(
            Human(user_id=agent.user_id, email="bot@example.com", password_hash="hash")
        ),
    )
    refused(lambda: session.add(Agent(user_id=human.user_id, owner_id=human.user_id)))
    refused(
        lambda: session.add(
            Human(user_id=agent.user_id, email="bot@example.com", password_hash="hash")
        )
    )
    assert session.get(Human, human.user_id) is not None
    assert session.get(Agent, human.user_id) is None
    assert session.get(Agent, agent.user_id) is not None
    assert session.get(Human, agent.user_id) is None


def test_the_service_acts_as_a_user_without_a_session(session):
    human = create_account(session, "Ada", "ada@example.com", "password-1")
    ada = session.get(User, human.user_id)
    agent = add_agent(session, ada, "Bot")
    issued = issue_key(session, ada, agent.user_id)
    assert agent.owner_id == human.user_id
    assert session.exec(select(Token)).all() == []
    assert exchange_key(session, issued.key)


def test_an_acting_agent_cannot_change_agents(session):
    human, secret = _account(session)
    agent, issued = _live_agent(session, secret)
    agent_secret = exchange_key(session, issued.key)
    calls = (
        lambda: add_agent(session, _actor(session, agent_secret), "Other"),
        lambda: issue_key(session, _actor(session, agent_secret), agent.user_id),
        lambda: roll_key(session, _actor(session, agent_secret), agent.user_id),
        lambda: retire_agent(session, _actor(session, agent_secret), agent.user_id),
        lambda: reassign_agent(session, _actor(session, agent_secret), agent.user_id),
        lambda: delete_agent(session, _actor(session, agent_secret), agent.user_id),
    )
    for call in calls:
        with pytest.raises(ServiceError):
            call()
    assert session.get(User, human.user_id) is not None
    assert session.get(Agent, agent.user_id) is not None
    assert session.get(Human, human.user_id) is not None


def test_a_human_cannot_change_another_accounts_agent(session):
    _human, secret = _account(session)
    bea_id, bea_secret = _second_account(session)
    bea_bot, bea_key = _live_agent(session, bea_secret, "Bea bot")
    bea_bare = add_agent(session, _actor(session, bea_secret), "Bea bare")
    bea_bot_secret = exchange_key(session, bea_key.key)
    session.commit()
    calls = (
        lambda: issue_key(session, _actor(session, secret), bea_bare.user_id),
        lambda: roll_key(session, _actor(session, secret), bea_bot.user_id),
        lambda: retire_agent(session, _actor(session, secret), bea_bot.user_id),
        lambda: delete_agent(session, _actor(session, secret), bea_bare.user_id),
        lambda: reassign_agent(session, _actor(session, secret), bea_bot.user_id),
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
    human, secret = _account(session)
    agent, issued = _live_agent(session, secret)
    agent_secret = exchange_key(session, issued.key)
    rolled = roll_key(session, _actor(session, secret), agent.user_id)
    assert authenticate(session, secret).id == human.user_id
    with pytest.raises(ServiceError):
        authenticate(session, agent_secret)
    assert session.exec(select(Token).where(Token.user_id == agent.user_id)).first() is None
    agent_secret = exchange_key(session, rolled.key)
    assert session.exec(select(Token).where(Token.user_id == agent.user_id)).one()
    retire_agent(session, _actor(session, secret), agent.user_id)
    with pytest.raises(ServiceError):
        authenticate(session, agent_secret)
    with pytest.raises(ServiceError):
        exchange_key(session, rolled.key)
    assert authenticate(session, secret).id == human.user_id
    session.refresh(agent)
    assert agent.retired_at is not None
    assert agent.key_hash is None
    assert agent.key_expires_at is None
    assert session.get(User, agent.user_id) is not None


def test_roll_with_no_session_leaves_none(session):
    _human, secret = _account(session)
    agent, issued = _live_agent(session, secret)
    roll_key(session, _actor(session, secret), agent.user_id)
    assert session.exec(select(Token).where(Token.user_id == agent.user_id)).first() is None


def test_retire_rolls_back_with_the_transaction(session):
    _human, secret = _account(session)
    agent, issued = _live_agent(session, secret)
    agent_secret = exchange_key(session, issued.key)
    session.commit()
    retire_agent(session, _actor(session, secret), agent.user_id)
    session.rollback()
    kept = session.get(Agent, agent.user_id)
    assert kept is not None
    assert kept.retired_at is None
    assert kept.key_hash is not None
    assert authenticate(session, agent_secret).id == agent.user_id


def test_delete_removes_an_agent_with_nothing_attached(session):
    human, secret = _account(session)
    agent, issued = _live_agent(session, secret)
    exchange_key(session, issued.key)
    delete_agent(session, _actor(session, secret), agent.user_id)
    assert session.get(Agent, agent.user_id) is None
    assert session.get(User, agent.user_id) is None
    assert session.exec(select(Token).where(Token.user_id == agent.user_id)).first() is None
    assert session.get(Human, human.user_id) is not None


def test_delete_does_nothing_when_data_is_attached(session):
    human, secret = _account(session)
    agent = add_agent(session, _actor(session, secret), "Bot")
    _attach(session, agent.user_id, human.user_id)
    delete_agent(session, _actor(session, secret), agent.user_id)
    assert session.get(Agent, agent.user_id) is not None
    assert session.get(User, agent.user_id) is not None
    task = session.connection().execute(text("SELECT created_by, owned_by FROM task")).one()
    assert task == (agent.user_id, human.user_id)


def test_reassign_moves_references_and_keeps_owned_by(session):
    human, secret = _account(session)
    agent, issued = _live_agent(session, secret)
    exchange_key(session, issued.key)
    _attach(session, agent.user_id, human.user_id)
    reassign_agent(session, _actor(session, secret), agent.user_id)
    assert session.get(Agent, agent.user_id) is None
    assert session.get(User, agent.user_id) is None
    assert session.get(Human, human.user_id) is not None
    task = session.connection().execute(
        text("SELECT created_by, updated_by, owned_by FROM task")
    ).one()
    block = session.connection().execute(
        text("SELECT created_by, released_by FROM block")
    ).one()
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


def test_a_missing_agent_is_refused(session):
    _human, secret = _account(session)
    with pytest.raises(ServiceError):
        issue_key(session, _actor(session, secret), 9999)


def test_reassign_with_nothing_attached_removes_the_agent(session):
    human, secret = _account(session)
    agent = add_agent(session, _actor(session, secret), "Bot")
    reassign_agent(session, _actor(session, secret), agent.user_id)
    assert session.get(User, agent.user_id) is None
    assert session.get(Human, human.user_id) is not None


def _update_agent(session: Session, user_id: int, **values: object) -> None:
    _update_row(session, "agent", user_id, values)


def _update_row(session: Session, table: str, user_id: int, values: dict[str, object]) -> None:
    assignments = ", ".join(f"{column} = :{column}" for column in values)
    session.connection().execute(
        text(f"UPDATE {table} SET {assignments} WHERE user_id = :user_id"),
        {"user_id": user_id, **values},
    )
    session.expire_all()


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
    connection.execute(
        text("CREATE TABLE task_history (id INTEGER PRIMARY KEY, user_id INTEGER)")
    )
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
