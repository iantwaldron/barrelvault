import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from barrelvault.models import Agent, Human, Token, User
from tests.support import KEY_LENGTH


def _user(session: Session, name: str) -> User:
    user = User(name=name)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _human(session: Session, name: str = "Ada", email: str = "ada@example.com") -> Human:
    """Insert a human directly. Only setup adds one through the ORM."""
    user = _user(session, name)
    _insert_human(session, user.id, email)
    session.commit()
    return session.get(Human, user.id)


def _insert_human(session: Session, user_id: int, email: str) -> None:
    session.connection().execute(
        text("INSERT INTO human (user_id, email, password_hash) VALUES (:user_id, :email, 'hash')"),
        {"user_id": user_id, "email": email},
    )


def _agent(session: Session, owner: Human, name: str, **fields) -> Agent:
    user = _user(session, name)
    agent = Agent(user_id=user.id, owner_id=owner.user_id, **fields)
    session.add(agent)
    session.commit()
    session.refresh(agent)
    return agent


def test_an_agent_name_is_unique_in_its_account(session):
    ada = _human(session)
    bea = _human(session, name="Bea", email="bea@example.com")
    _agent(session, ada, "Bot")
    for taken in ("bot", "BOT", "ada"):
        user = _user(session, taken)
        with pytest.raises(IntegrityError):
            session.connection().execute(
                text("INSERT INTO agent (user_id, owner_id) VALUES (:user_id, :owner_id)"),
                {"user_id": user.id, "owner_id": ada.user_id},
            )
        session.rollback()
    # Another account may use the same name, and a plain user row is not checked.
    assert _agent(session, bea, "Bot").owner_id == bea.user_id
    assert _user(session, "Bot").name == "Bot"


def test_a_rename_keeps_names_unique_in_the_account(session):
    ada = _human(session)
    bea = _human(session, name="Bea", email="bea@example.com")
    bot = _agent(session, ada, "Bot")
    other = _agent(session, ada, "Other")
    _agent(session, bea, "Helper")

    def rename(user_id: int, name: str) -> None:
        session.connection().execute(
            text("UPDATE user SET name = :name WHERE id = :id"), {"id": user_id, "name": name}
        )

    for user_id, taken in ((other.user_id, "BOT"), (other.user_id, "Ada"), (ada.user_id, "bot")):
        with pytest.raises(IntegrityError):
            rename(user_id, taken)
        session.rollback()
    rename(other.user_id, "Helper")
    rename(bot.user_id, "bot")
    session.commit()
    assert session.get(User, other.user_id).name == "Helper"


def test_human_email_is_unique(session):
    _human(session)
    other = _user(session, "Other")
    with pytest.raises(IntegrityError):
        _insert_human(session, other.id, "ada@example.com")


def test_human_requires_a_user(session):
    with pytest.raises(IntegrityError):
        _insert_human(session, 1, "ada@example.com")


def test_agent_user_must_exist(session):
    human = _human(session)
    session.add(Agent(user_id=human.user_id + 1, owner_id=human.user_id))
    with pytest.raises(IntegrityError):
        session.commit()


def test_agent_owner_must_exist(session):
    user = _user(session, "Bot")
    with pytest.raises(IntegrityError):
        session.connection().execute(
            text("INSERT INTO agent (user_id, owner_id) VALUES (:user_id, :owner_id)"),
            {"user_id": user.id, "owner_id": user.id + 1},
        )


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
        (
            "INSERT INTO human (user_id, email, password_hash) "
            "VALUES (:user_id, 'ada@example.com', NULL)"
        ),
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


def test_generate_key_stores_the_hash_and_expiry(session):
    owner = _human(session)
    user = _user(session, "Bot")
    agent = Agent(user_id=user.id, owner_id=owner.user_id)
    before = datetime.now(UTC)
    raw = agent.generate_key(100)
    after = datetime.now(UTC)
    session.add(agent)
    session.commit()
    session.refresh(agent)

    assert len(bytes.fromhex(raw)) == 32
    assert agent.key_hash == hashlib.sha256(raw.encode()).hexdigest()
    assert raw != agent.key_hash
    stored = (
        session.connection()
        .execute(
            text("SELECT key_hash FROM agent WHERE user_id = :user_id"),
            {"user_id": agent.user_id},
        )
        .scalar_one()
    )
    assert stored == agent.key_hash
    assert raw not in stored
    assert agent.key_expires_at is not None
    assert before + timedelta(seconds=100) <= agent.key_expires_at
    assert agent.key_expires_at <= after + timedelta(seconds=100)


def test_generate_key_replaces_the_previous_key(session):
    agent = _agent(session, _human(session), "Bot")
    first = agent.generate_key(KEY_LENGTH)
    second = agent.generate_key(KEY_LENGTH)
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


def test_mint_stores_the_hash_and_expiry(session):
    user = _user(session, "Ada")
    before = datetime.now(UTC)
    token, raw = Token.mint(user.id, 100)
    after = datetime.now(UTC)
    session.add(token)
    session.commit()
    session.refresh(token)

    assert len(bytes.fromhex(raw)) == 32
    assert token.secret_hash == hashlib.sha256(raw.encode()).hexdigest()
    assert raw != token.secret_hash
    assert before <= token.created_at <= after
    assert token.expires_at == token.created_at + timedelta(seconds=100)
    stored = (
        session.connection()
        .execute(
            text("SELECT secret_hash FROM token WHERE user_id = :user_id"),
            {"user_id": user.id},
        )
        .scalar_one()
    )
    assert stored == token.secret_hash
    assert raw not in stored


def test_each_mint_has_a_new_secret(session):
    user_id = _user(session, "Ada").id
    first, first_raw = Token.mint(user_id, 100)
    second, second_raw = Token.mint(user_id, 100)
    assert first_raw != second_raw
    assert first.secret_hash != second.secret_hash
    assert second.secret_hash == hashlib.sha256(second_raw.encode()).hexdigest()
    assert second.expires_at == second.created_at + timedelta(seconds=100)
