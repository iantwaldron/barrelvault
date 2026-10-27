import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from barrelvault.models import Agent, Human, Token, User


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
    with Session(engine) as db:
        yield db


def _user(session: Session, name: str) -> User:
    user = User(name=name)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _human(session: Session, name: str = "Ada", email: str = "ada@example.com") -> Human:
    user = _user(session, name)
    human = Human(user_id=user.id, email=email, password_hash="hash")
    session.add(human)
    session.commit()
    session.refresh(human)
    return human


def _agent(session: Session, owner: Human, name: str, **fields) -> Agent:
    user = _user(session, name)
    agent = Agent(user_id=user.id, owner_id=owner.user_id, **fields)
    session.add(agent)
    session.commit()
    session.refresh(agent)
    return agent


def test_user_name_is_unique(session):
    _user(session, "Ada")
    session.add(User(name="Ada"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_human_email_is_unique(session):
    _human(session)
    other = _user(session, "Other")
    session.add(Human(user_id=other.id, email="ada@example.com", password_hash="hash"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_human_requires_a_user(session):
    session.add(Human(user_id=1, email="ada@example.com", password_hash="hash"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_agent_user_must_exist(session):
    human = _human(session)
    session.add(Agent(user_id=human.user_id + 1, owner_id=human.user_id))
    with pytest.raises(IntegrityError):
        session.commit()


def test_agent_owner_must_exist(session):
    user = _user(session, "Bot")
    session.add(Agent(user_id=user.id, owner_id=user.id + 1))
    with pytest.raises(IntegrityError):
        session.commit()


def test_one_human_profile_per_user(session):
    human = _human(session)
    with pytest.raises(IntegrityError):
        session.connection().execute(
            text(
                "INSERT INTO human (user_id, email, password_hash) "
                "VALUES (:user_id, 'other@example.com', 'hash')"
            ),
            {"user_id": human.user_id},
        )


def test_one_agent_profile_per_user(session):
    human = _human(session)
    agent = _agent(session, human, "Bot")
    with pytest.raises(IntegrityError):
        session.connection().execute(
            text("INSERT INTO agent (user_id, owner_id) VALUES (:user_id, :owner_id)"),
            {"user_id": agent.user_id, "owner_id": human.user_id},
        )


def test_required_columns_reject_null(session):
    user = _user(session, "Ada")
    created = datetime.now(UTC).isoformat()
    statements = [
        "INSERT INTO user (name) VALUES (NULL)",
        "INSERT INTO human (user_id, email, password_hash) VALUES (:user_id, NULL, 'hash')",
        "INSERT INTO human (user_id, email, password_hash) VALUES (:user_id, 'ada@example.com', NULL)",
        "INSERT INTO agent (user_id, owner_id) VALUES (:user_id, NULL)",
        (
            "INSERT INTO token (user_id, secret_hash, created_at, expires_at) "
            "VALUES (:user_id, NULL, :created, :created)"
        ),
        (
            "INSERT INTO token (user_id, secret_hash, created_at, expires_at) "
            "VALUES (:user_id, 'hash', NULL, :created)"
        ),
    ]
    for statement in statements:
        with pytest.raises(IntegrityError):
            session.connection().execute(
                text(statement),
                {"user_id": user.id, "created": created},
            )
        session.rollback()


def test_null_key_hash_is_read_back(session):
    connection = session.connection()
    connection.execute(text("INSERT INTO user (name) VALUES ('Ada')"))
    connection.execute(text("INSERT INTO user (name) VALUES ('Bot')"))
    connection.execute(
        text(
            "INSERT INTO human (user_id, email, password_hash) "
            "VALUES (1, 'ada@example.com', 'hash')"
        )
    )
    connection.execute(
        text(
            "INSERT INTO agent (user_id, owner_id, key_hash, key_expires_at, retired_at) "
            "VALUES (2, 1, NULL, NULL, NULL)"
        )
    )
    session.commit()

    agent = session.get(Agent, 2)
    assert agent is not None
    assert agent.key_hash is None
    assert agent.key_expires_at is None
    assert agent.retired_at is None


def test_get_agents_is_empty_without_agents(session):
    assert _human(session).get_agents(session) == []


def test_agents_include_retired(session):
    human = _human(session)
    live = _agent(session, human, "Live")
    retired = _agent(session, human, "Retired", retired_at=datetime.now(UTC))
    other = _human(session, name="Bea", email="bea@example.com")
    _agent(session, other, "Other bot")

    found = human.get_agents(session)
    assert {agent.user_id for agent in found} == {live.user_id, retired.user_id}


def test_generate_key_stores_the_hash_and_expiry(session, monkeypatch):
    monkeypatch.setenv("BARRELVAULT_KEY_LENGTH", "100")
    owner = _human(session)
    user = _user(session, "Bot")
    agent = Agent(user_id=user.id, owner_id=owner.user_id, retired_at=datetime.now(UTC))
    retired_at = agent.retired_at
    before = datetime.now(UTC)
    raw = agent.generate_key()
    after = datetime.now(UTC)
    session.add(agent)
    session.commit()
    session.refresh(agent)

    assert len(bytes.fromhex(raw)) == 32
    assert agent.key_hash == hashlib.sha256(raw.encode()).hexdigest()
    assert raw != agent.key_hash
    stored = session.connection().execute(
        text("SELECT key_hash FROM agent WHERE user_id = :user_id"),
        {"user_id": agent.user_id},
    ).scalar_one()
    assert stored == agent.key_hash
    assert raw not in stored
    assert agent.key_expires_at is not None
    assert before + timedelta(seconds=100) <= agent.key_expires_at
    assert agent.key_expires_at <= after + timedelta(seconds=100)
    assert agent.retired_at == retired_at


def test_generate_key_uses_the_default_length(session, monkeypatch):
    monkeypatch.delenv("BARRELVAULT_KEY_LENGTH", raising=False)
    agent = _agent(session, _human(session), "Bot")
    before = datetime.now(UTC)
    agent.generate_key()
    after = datetime.now(UTC)
    assert agent.key_expires_at is not None
    assert before + timedelta(seconds=31536000) <= agent.key_expires_at
    assert agent.key_expires_at <= after + timedelta(seconds=31536000)


def test_generate_key_replaces_the_previous_key(session, monkeypatch):
    monkeypatch.setenv("BARRELVAULT_KEY_LENGTH", "100")
    agent = _agent(session, _human(session), "Bot")
    first = agent.generate_key()
    second = agent.generate_key()
    assert first != second
    assert agent.key_hash == hashlib.sha256(second.encode()).hexdigest()
    assert agent.key_hash != hashlib.sha256(first.encode()).hexdigest()


def test_token_requires_a_user(session):
    session.add(
        Token(
            user_id=1,
            secret_hash="hash",
            expires_at=datetime.now(UTC),
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_two_users_each_have_a_session(session):
    now = datetime.now(UTC)
    ada = _user(session, "Ada")
    bea = _user(session, "Bea")
    session.add(Token(user_id=ada.id, secret_hash="one", expires_at=now))
    session.add(Token(user_id=bea.id, secret_hash="two", expires_at=now))
    session.commit()
    found = session.exec(select(Token)).all()
    assert {token.user_id for token in found} == {ada.id, bea.id}


def test_one_session_per_user(session):
    user = _user(session, "Ada")
    now = datetime.now(UTC)
    session.add(Token(user_id=user.id, secret_hash="one", expires_at=now))
    session.commit()
    session.add(Token(user_id=user.id, secret_hash="two", expires_at=now))
    with pytest.raises(IntegrityError):
        session.commit()


def test_secret_hash_is_unique(session):
    now = datetime.now(UTC)
    session.add(Token(user_id=_user(session, "Ada").id, secret_hash="same", expires_at=now))
    session.commit()
    session.add(Token(user_id=_user(session, "Bea").id, secret_hash="same", expires_at=now))
    with pytest.raises(IntegrityError):
        session.commit()


def test_created_at_is_set_on_insert(session):
    user = _user(session, "Ada")
    before = datetime.now(UTC)
    token = Token(user_id=user.id, secret_hash="hash", expires_at=before)
    session.add(token)
    session.commit()
    session.refresh(token)
    after = datetime.now(UTC)
    assert before <= token.created_at <= after


def test_expires_at_is_required(session):
    user = _user(session, "Ada")
    connection = session.connection()
    with pytest.raises(IntegrityError):
        connection.execute(
            text(
                "INSERT INTO token (user_id, secret_hash, created_at, expires_at) "
                "VALUES (:user_id, 'hash', :created_at, NULL)"
            ),
            {"user_id": user.id, "created_at": datetime.now(UTC).isoformat()},
        )


def test_generate_secret_stores_the_hash_and_expiry(session, monkeypatch):
    monkeypatch.setenv("BARRELVAULT_SESSION_LENGTH", "100")
    user = _user(session, "Ada")
    token = Token(user_id=user.id)
    before = datetime.now(UTC)
    raw = token.generate_secret()
    after = datetime.now(UTC)
    session.add(token)
    session.commit()
    session.refresh(token)

    assert len(bytes.fromhex(raw)) == 32
    assert token.secret_hash == hashlib.sha256(raw.encode()).hexdigest()
    assert raw != token.secret_hash
    assert before <= token.created_at <= after
    assert token.expires_at == token.created_at + timedelta(seconds=100)
    stored = session.connection().execute(
        text("SELECT secret_hash FROM token WHERE user_id = :user_id"),
        {"user_id": user.id},
    ).scalar_one()
    assert stored == token.secret_hash
    assert raw not in stored


def test_generate_secret_uses_the_default_length(session, monkeypatch):
    monkeypatch.delenv("BARRELVAULT_SESSION_LENGTH", raising=False)
    token = Token(user_id=_user(session, "Ada").id)
    before = datetime.now(UTC)
    token.generate_secret()
    after = datetime.now(UTC)
    assert before + timedelta(seconds=604800) <= token.expires_at
    assert token.expires_at <= after + timedelta(seconds=604800)


def test_generate_secret_replaces_the_previous_secret(session, monkeypatch):
    monkeypatch.setenv("BARRELVAULT_SESSION_LENGTH", "100")
    token = Token(user_id=_user(session, "Ada").id)
    first = token.generate_secret()
    second = token.generate_secret()
    assert first != second
    assert token.secret_hash == hashlib.sha256(second.encode()).hexdigest()
    assert token.secret_hash != hashlib.sha256(first.encode()).hexdigest()
    assert token.expires_at == token.created_at + timedelta(seconds=100)
