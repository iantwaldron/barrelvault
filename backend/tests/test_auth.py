import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from barrelvault import auth, db
from barrelvault.account import IssuedKey, add_agent, create_account, issue_key, retire_agent
from barrelvault.auth import (
    ServiceError,
    authenticate,
    exchange_key,
    login,
    logout,
)
from barrelvault.models import Agent, Human, Token, User
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


def _account(session: Session) -> tuple[Human, str]:
    human = create_account(session, "Ada", "ada@example.com", "password-1")
    secret = login(session, "ada@example.com", "password-1")
    return human, secret


def _live_agent(session: Session, secret: str, name: str = "Bot") -> tuple[Agent, IssuedKey]:
    agent = add_agent(session, secret, name)
    issued = issue_key(session, secret, agent.user_id)
    return agent, issued


def _tokens(session: Session) -> list[Token]:
    return list(session.exec(select(Token)).all())


def test_login_and_exchange_store_the_actor_and_expiry(session, monkeypatch):
    monkeypatch.setenv("BARRELVAULT_SESSION_LENGTH", "100")
    before = datetime.now(UTC)
    human, secret = _account(session)
    agent, issued = _live_agent(session, secret)
    agent_secret = exchange_key(session, issued.key)
    after = datetime.now(UTC)
    rows = {token.user_id: token for token in _tokens(session)}
    assert set(rows) == {human.user_id, agent.user_id}
    for token in rows.values():
        assert before + timedelta(seconds=100) <= token.expires_at
        assert token.expires_at <= after + timedelta(seconds=100)
    assert authenticate(session, secret).id == human.user_id
    assert authenticate(session, agent_secret).id == agent.user_id


def test_login_uses_the_default_session_length(session, monkeypatch):
    monkeypatch.delenv("BARRELVAULT_SESSION_LENGTH", raising=False)
    create_account(session, "Ada", "ada@example.com", "password-1")
    before = datetime.now(UTC)
    login(session, "ada@example.com", "password-1")
    after = datetime.now(UTC)
    token = _tokens(session)[0]
    assert before + timedelta(seconds=604800) <= token.expires_at
    assert token.expires_at <= after + timedelta(seconds=604800)


def test_wrong_email_and_wrong_password_are_the_same_failure(session, monkeypatch):
    create_account(session, "Ada", "ada@example.com", "password-1")
    calls = {"n": 0}
    real = auth._passwords.verify

    def verify(password, hashed):
        calls["n"] += 1
        return real(password, hashed)

    monkeypatch.setattr(auth._passwords, "verify", verify)
    with pytest.raises(ServiceError):
        login(session, "missing@example.com", "password-1")
    assert calls["n"] == 1
    calls["n"] = 0
    with pytest.raises(ServiceError):
        login(session, "ada@example.com", "wrong-password")
    assert calls["n"] == 1
    assert _tokens(session) == []


def test_an_unrecognized_password_hash_is_a_refused_login(session):
    human = create_account(session, "Ada", "ada@example.com", "password-1")
    session.connection().execute(
        text("UPDATE human SET password_hash = 'password-1' WHERE user_id = :user_id"),
        {"user_id": human.user_id},
    )
    session.expire_all()
    with pytest.raises(ServiceError):
        login(session, "ada@example.com", "password-1")
    assert _tokens(session) == []


def test_refused_mint_leaves_the_existing_session(session):
    create_account(session, "Ada", "ada@example.com", "password-1")
    secret = login(session, "ada@example.com", "password-1")
    token_id = _tokens(session)[0].id
    agent, issued = _live_agent(session, secret)
    with pytest.raises(ServiceError):
        login(session, "ada@example.com", "wrong-password")
    with pytest.raises(ServiceError):
        login(session, secret, "password-1")
    with pytest.raises(ServiceError):
        exchange_key(session, secret)
    with pytest.raises(ServiceError):
        exchange_key(session, "missing-key")
    _update_agent(
        session,
        agent.user_id,
        key_expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
    )
    with pytest.raises(ServiceError):
        exchange_key(session, issued.key)
    _update_agent(session, agent.user_id, key_hash=None, key_expires_at=None)
    with pytest.raises(ServiceError):
        exchange_key(session, issued.key)
    retire_agent(session, secret, agent.user_id)
    with pytest.raises(ServiceError):
        exchange_key(session, issued.key)
    human_token = session.exec(select(Token).where(Token.user_id != agent.user_id)).one()
    assert human_token.id == token_id
    assert authenticate(session, secret).name == "Ada"


def test_a_mint_that_does_not_commit_leaves_the_previous_session(session):
    create_account(session, "Ada", "ada@example.com", "password-1")
    secret = login(session, "ada@example.com", "password-1")
    session.commit()
    login(session, "ada@example.com", "password-1")
    session.rollback()
    assert authenticate(session, secret).name == "Ada"
    assert len(_tokens(session)) == 1


def test_a_second_mint_replaces_the_row(session):
    updates: list[bool] = []

    def _updated(*_args: object) -> None:
        updates.append(True)

    event.listen(Token, "before_update", _updated)
    try:
        create_account(session, "Ada", "ada@example.com", "password-1")
        first = login(session, "ada@example.com", "password-1")
        first_hash = _tokens(session)[0].secret_hash
        second = login(session, "ada@example.com", "password-1")
        rows = _tokens(session)
        assert updates == []
        assert len(rows) == 1
        assert rows[0].secret_hash != first_hash
        assert authenticate(session, second).name == "Ada"
        with pytest.raises(ServiceError):
            authenticate(session, first)
    finally:
        event.remove(Token, "before_update", _updated)


def test_invalid_session_length_refuses_before_delete(session, monkeypatch):
    create_account(session, "Ada", "ada@example.com", "password-1")
    secret = login(session, "ada@example.com", "password-1")
    monkeypatch.setenv("BARRELVAULT_SESSION_LENGTH", "nope")
    with pytest.raises(SettingsError):
        login(session, "ada@example.com", "password-1")
    monkeypatch.delenv("BARRELVAULT_SESSION_LENGTH", raising=False)
    assert authenticate(session, secret).name == "Ada"


def test_a_mint_without_an_expiry_is_refused(session, monkeypatch):
    create_account(session, "Ada", "ada@example.com", "password-1")
    secret = login(session, "ada@example.com", "password-1")
    session.commit()

    def generate_secret(self) -> str:
        self.secret_hash = "unfinished"
        return "raw"

    monkeypatch.setattr(auth.Token, "generate_secret", generate_secret)
    with pytest.raises(ServiceError):
        login(session, "ada@example.com", "password-1")
    session.rollback()
    assert session.exec(select(Token).where(Token.secret_hash == "unfinished")).first() is None
    assert authenticate(session, secret).name == "Ada"


def test_session_field_changes_are_refused(session):
    create_account(session, "Ada", "ada@example.com", "password-1")
    secret = login(session, "ada@example.com", "password-1")
    session.commit()
    token = _tokens(session)[0]
    original_expiry = token.expires_at
    original_hash = token.secret_hash
    original_user = token.user_id
    token.expires_at = None
    with pytest.raises(ServiceError):
        session.flush()
    session.rollback()
    token = session.exec(select(Token).where(Token.secret_hash == original_hash)).one()
    token.expires_at = original_expiry + timedelta(seconds=1)
    with pytest.raises(ServiceError):
        session.flush()
    token.secret_hash = "other"
    with pytest.raises(ServiceError):
        session.flush()
    token.user_id = original_user + 1
    with pytest.raises(ServiceError):
        session.flush()
    session.rollback()
    kept = _tokens(session)[0]
    assert kept.expires_at == original_expiry
    assert kept.secret_hash == original_hash
    assert kept.user_id == original_user
    assert authenticate(session, secret).id == original_user


def test_authenticate_refuses_unknown_and_expired_secrets(session):
    create_account(session, "Ada", "ada@example.com", "password-1")
    secret = login(session, "ada@example.com", "password-1")
    with pytest.raises(ServiceError):
        authenticate(session, "missing")
    with pytest.raises(ServiceError):
        authenticate(session, _issued_key(session, secret))
    token = _tokens(session)[0]
    _update_token(
        session,
        token.user_id,
        expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
    )
    with pytest.raises(ServiceError):
        authenticate(session, secret)
    assert len(_tokens(session)) == 1
    login(session, "ada@example.com", "password-1")
    assert len(_tokens(session)) == 1
    with pytest.raises(ServiceError):
        authenticate(session, secret)


def test_authenticate_refuses_a_session_whose_user_is_gone(session):
    human = create_account(session, "Ada", "ada@example.com", "password-1")
    secret = login(session, "ada@example.com", "password-1")
    session.commit()
    connection = session.connection()
    connection.execute(text("PRAGMA foreign_keys=OFF"))
    connection.execute(
        text("DELETE FROM user WHERE id = :user_id"),
        {"user_id": human.user_id},
    )
    session.expunge_all()
    with pytest.raises(ServiceError):
        authenticate(session, secret)
    assert len(_tokens(session)) == 1


def test_a_dead_agent_key_deletes_that_session_only(session):
    human, secret = _account(session)
    agent, issued = _live_agent(session, secret)
    agent_secret = exchange_key(session, issued.key)
    _update_agent(session, agent.user_id, key_hash=None, key_expires_at=None)
    session.commit()
    with pytest.raises(ServiceError):
        authenticate(session, agent_secret)
    assert session.exec(select(Token).where(Token.user_id == agent.user_id)).first() is None
    assert authenticate(session, secret).id == human.user_id


def test_a_retired_agent_session_is_deleted_and_refused(session):
    human, secret = _account(session)
    agent, issued = _live_agent(session, secret)
    agent_secret = exchange_key(session, issued.key)
    _update_agent(session, agent.user_id, retired_at=datetime.now(UTC).isoformat())
    session.commit()
    with pytest.raises(ServiceError):
        authenticate(session, agent_secret)
    assert session.exec(select(Token).where(Token.user_id == agent.user_id)).first() is None
    assert authenticate(session, secret).id == human.user_id


def test_dead_agent_key_stays_deleted_when_the_request_rolls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("BARRELVAULT_DB", str(tmp_path / "barrelvault.db"))
    monkeypatch.delenv("BARRELVAULT_SESSION_LENGTH", raising=False)
    monkeypatch.delenv("BARRELVAULT_KEY_LENGTH", raising=False)
    _reset_engine()
    SQLModel.metadata.create_all(db.get_engine())
    with db.open_session() as db_session:
        _human, secret = _account(db_session)
        agent, issued = _live_agent(db_session, secret)
        agent_secret = exchange_key(db_session, issued.key)
        agent_id = agent.user_id
    with db.open_session() as db_session:
        _update_agent(
            db_session,
            agent_id,
            key_expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        )
    with pytest.raises(ServiceError):
        with db.open_session() as db_session:
            db_session.add(User(name="Unsaved"))
            db_session.flush()
            authenticate(db_session, agent_secret)
    with db.open_session() as db_session:
        assert db_session.exec(select(Token).where(Token.user_id == agent_id)).first() is None
        assert db_session.exec(select(User).where(User.name == "Unsaved")).first() is None
        assert authenticate(db_session, secret).name == "Ada"
    _reset_engine()


def test_dead_agent_key_delete_survives_a_session_closed_without_commit(session):
    _human, secret = _account(session)
    agent, issued = _live_agent(session, secret)
    agent_secret = exchange_key(session, issued.key)
    _update_agent(session, agent.user_id, key_hash=None, key_expires_at=None)
    session.commit()
    request = Session(session.get_bind())
    request.add(User(name="Unsaved"))
    with pytest.raises(ServiceError):
        authenticate(request, agent_secret)
    request.close()
    assert session.exec(select(Token).where(Token.user_id == agent.user_id)).first() is None
    assert session.exec(select(User).where(User.name == "Unsaved")).first() is None


def test_dead_agent_key_delete_commits_with_the_caller(session):
    _human, secret = _account(session)
    agent, issued = _live_agent(session, secret)
    agent_secret = exchange_key(session, issued.key)
    _update_agent(session, agent.user_id, key_hash=None, key_expires_at=None)
    session.commit()
    with pytest.raises(ServiceError):
        authenticate(session, agent_secret)
    session.begin_nested().rollback()
    session.commit()
    assert auth._DEAD_SESSIONS not in session.info
    assert session.exec(select(Token).where(Token.user_id == agent.user_id)).first() is None


def test_blank_secrets_are_refused(session):
    create_account(session, "Ada", "ada@example.com", "password-1")
    for secret in (" ", None):
        with pytest.raises(ServiceError):
            exchange_key(session, secret)
        with pytest.raises(ServiceError):
            authenticate(session, secret)
        logout(session, secret)
    assert _tokens(session) == []


def test_a_new_session_without_expiry_is_refused(session):
    human = create_account(session, "Ada", "ada@example.com", "password-1")
    session.add(Token(user_id=human.user_id, secret_hash="hash"))
    with pytest.raises(ServiceError):
        session.flush()


def test_a_naive_expiry_is_still_compared_in_utc(session):
    create_account(session, "Ada", "ada@example.com", "password-1")
    secret = login(session, "ada@example.com", "password-1")
    token = _tokens(session)[0]
    _update_token(session, token.user_id, expires_at="2000-01-01 00:00:00")
    with pytest.raises(ServiceError):
        authenticate(session, secret)
    assert len(_tokens(session)) == 1


def test_a_naive_datetime_is_treated_as_utc():
    naive = datetime(2000, 1, 1)
    compared = auth._as_utc(naive)
    assert compared.tzinfo is UTC
    assert compared == datetime(2000, 1, 1, tzinfo=UTC)


def test_logout_deletes_the_row(session):
    create_account(session, "Ada", "ada@example.com", "password-1")
    secret = login(session, "ada@example.com", "password-1")
    logout(session, secret)
    assert _tokens(session) == []
    with pytest.raises(ServiceError):
        authenticate(session, secret)
    logout(session, secret)
    assert _tokens(session) == []


def test_overlapping_mints_leave_one_session(tmp_path, monkeypatch):
    monkeypatch.setenv("BARRELVAULT_DB", str(tmp_path / "barrelvault.db"))
    monkeypatch.delenv("BARRELVAULT_SESSION_LENGTH", raising=False)
    monkeypatch.delenv("BARRELVAULT_KEY_LENGTH", raising=False)
    _reset_engine()
    SQLModel.metadata.create_all(db.get_engine())
    with db.open_session() as db_session:
        human = create_account(db_session, "Ada", "ada@example.com", "password-1")
        user_id = human.user_id
    barrier = threading.Barrier(2)
    found: list[str] = []
    errors: list[BaseException] = []

    def mint() -> None:
        try:
            with db.open_session() as db_session:
                barrier.wait(timeout=5)
                found.append(auth._mint(db_session, user_id))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=mint), threading.Thread(target=mint)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(found) + len(errors) == 2
    with db.open_session() as db_session:
        rows = list(db_session.exec(select(Token)).all())
        assert len(rows) == 1
        accepted = []
        for raw in found:
            try:
                accepted.append(authenticate(db_session, raw).id)
            except ServiceError:
                pass
        assert accepted == [user_id]
    _reset_engine()


def _issued_key(session: Session, secret: str) -> str:
    _agent, issued = _live_agent(session, secret, "Keyed")
    return issued.key


def _update_agent(session: Session, user_id: int, **values: object) -> None:
    _update_row(session, "agent", user_id, values)


def _update_token(session: Session, user_id: int, **values: object) -> None:
    _update_row(session, "token", user_id, values)


def _update_row(session: Session, table: str, user_id: int, values: dict[str, object]) -> None:
    assignments = ", ".join(f"{column} = :{column}" for column in values)
    session.connection().execute(
        text(f"UPDATE {table} SET {assignments} WHERE user_id = :user_id"),
        {"user_id": user_id, **values},
    )
    session.expire_all()


def _reset_engine() -> None:
    if db._engine is not None:
        db._engine.dispose()
    db._engine = None
