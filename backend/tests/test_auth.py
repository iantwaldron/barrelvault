import sqlite3
import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, select

from barrelvault import auth, db
from barrelvault.account import create_account, retire_agent
from barrelvault.auth import (
    authenticate,
    exchange_key,
    login,
    logout,
)
from barrelvault.errors import Refused, ServiceError
from barrelvault.models import Token, User
from tests.support import (
    SESSION_LENGTH,
    act_as,
    live_agent,
    make_account,
    set_token_expiry,
    update_agent,
)


def _tokens(session: Session) -> list[Token]:
    return list(session.exec(select(Token)).all())


def test_login_and_exchange_store_the_actor_and_expiry(session):
    before = datetime.now(UTC)
    human = create_account(session, "Ada", "ada@example.com", "password-1")
    secret = login(session, "ada@example.com", "password-1", session_length=100)
    agent, issued = live_agent(session, secret)
    agent_secret = exchange_key(session, issued.key, session_length=100)
    after = datetime.now(UTC)
    rows = {token.user_id: token for token in _tokens(session)}
    assert set(rows) == {human.user_id, agent.user_id}
    for token in rows.values():
        assert before + timedelta(seconds=100) <= token.expires_at
        assert token.expires_at <= after + timedelta(seconds=100)
    assert authenticate(session, secret).id == human.user_id
    assert authenticate(session, agent_secret).id == agent.user_id


def test_login_matches_the_email_in_any_case(session):
    create_account(session, "Ada", "ada@example.com", "password-1")
    for typed in ("ADA@example.com", "Ada@Example.Com", " ada@example.com "):
        secret = login(session, typed, "password-1", session_length=SESSION_LENGTH)
        assert authenticate(session, secret).name == "Ada"


def test_wrong_email_and_wrong_password_are_the_same_failure(session, monkeypatch):
    create_account(session, "Ada", "ada@example.com", "password-1")
    calls = {"n": 0}
    real = auth._passwords.verify

    def verify(password, hashed):
        calls["n"] += 1
        return real(password, hashed)

    monkeypatch.setattr(auth._passwords, "verify", verify)
    with pytest.raises(ServiceError):
        login(session, "missing@example.com", "password-1", session_length=SESSION_LENGTH)
    assert calls["n"] == 1
    calls["n"] = 0
    with pytest.raises(Refused):
        login(session, "not an address", "password-1", session_length=SESSION_LENGTH)
    assert calls["n"] == 1
    calls["n"] = 0
    with pytest.raises(ServiceError):
        login(session, "ada@example.com", "wrong-password", session_length=SESSION_LENGTH)
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
        login(session, "ada@example.com", "password-1", session_length=SESSION_LENGTH)
    assert _tokens(session) == []


def test_every_auth_refusal_is_the_same(session):
    _human, secret = make_account(session)
    _agent, issued = live_agent(session, secret)
    calls = (
        lambda: login(session, "ada@example.com", "wrong", session_length=SESSION_LENGTH),
        lambda: login(session, "missing@example.com", "password-1", session_length=SESSION_LENGTH),
        lambda: exchange_key(session, "missing-key", session_length=SESSION_LENGTH),
        lambda: exchange_key(session, secret, session_length=SESSION_LENGTH),
        lambda: authenticate(session, issued.key),
        lambda: authenticate(session, " "),
    )
    for call in calls:
        with pytest.raises(Refused) as raised:
            call()
        assert raised.value.message == "refused"


def test_refused_mint_leaves_the_existing_session(session):
    create_account(session, "Ada", "ada@example.com", "password-1")
    secret = login(session, "ada@example.com", "password-1", session_length=SESSION_LENGTH)
    token_id = _tokens(session)[0].id
    agent, issued = live_agent(session, secret)
    with pytest.raises(ServiceError):
        login(session, "ada@example.com", "wrong-password", session_length=SESSION_LENGTH)
    with pytest.raises(ServiceError):
        login(session, secret, "password-1", session_length=SESSION_LENGTH)
    with pytest.raises(ServiceError):
        exchange_key(session, secret, session_length=SESSION_LENGTH)
    with pytest.raises(ServiceError):
        exchange_key(session, "missing-key", session_length=SESSION_LENGTH)
    update_agent(
        session,
        agent.user_id,
        key_expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
    )
    with pytest.raises(ServiceError):
        exchange_key(session, issued.key, session_length=SESSION_LENGTH)
    update_agent(session, agent.user_id, key_hash=None, key_expires_at=None)
    with pytest.raises(ServiceError):
        exchange_key(session, issued.key, session_length=SESSION_LENGTH)
    retire_agent(session, act_as(session, secret), agent.user_id)
    with pytest.raises(ServiceError):
        exchange_key(session, issued.key, session_length=SESSION_LENGTH)
    human_token = session.exec(select(Token).where(Token.user_id != agent.user_id)).one()
    assert human_token.id == token_id
    assert authenticate(session, secret).name == "Ada"


def test_a_mint_that_does_not_commit_leaves_the_previous_session(session):
    create_account(session, "Ada", "ada@example.com", "password-1")
    secret = login(session, "ada@example.com", "password-1", session_length=SESSION_LENGTH)
    session.commit()
    login(session, "ada@example.com", "password-1", session_length=SESSION_LENGTH)
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
        first = login(session, "ada@example.com", "password-1", session_length=SESSION_LENGTH)
        first_hash = _tokens(session)[0].secret_hash
        second = login(session, "ada@example.com", "password-1", session_length=SESSION_LENGTH)
        rows = _tokens(session)
        assert updates == []
        assert len(rows) == 1
        assert rows[0].secret_hash != first_hash
        assert authenticate(session, second).name == "Ada"
        with pytest.raises(ServiceError):
            authenticate(session, first)
    finally:
        event.remove(Token, "before_update", _updated)


def test_a_mint_without_an_expiry_is_refused(session, monkeypatch):
    create_account(session, "Ada", "ada@example.com", "password-1")
    secret = login(session, "ada@example.com", "password-1", session_length=SESSION_LENGTH)
    session.commit()

    def mint(user_id: int, _session_length: int) -> tuple[Token, str]:
        return Token(user_id=user_id, secret_hash="unfinished", expires_at=None), "raw"

    monkeypatch.setattr(auth.Token, "mint", mint)
    with pytest.raises(IntegrityError):
        login(session, "ada@example.com", "password-1", session_length=SESSION_LENGTH)
    session.rollback()
    assert session.exec(select(Token).where(Token.secret_hash == "unfinished")).first() is None
    assert authenticate(session, secret).name == "Ada"


def test_session_field_changes_are_refused(session):
    create_account(session, "Ada", "ada@example.com", "password-1")
    secret = login(session, "ada@example.com", "password-1", session_length=SESSION_LENGTH)
    session.commit()
    original = _tokens(session)[0]
    changes = {
        "expires_at": None,
        "secret_hash": "other",
        "user_id": original.user_id + 1,
    }
    for name, value in changes.items():
        token = _tokens(session)[0]
        setattr(token, name, value)
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()
    token = _tokens(session)[0]
    token.expires_at = original.expires_at + timedelta(seconds=1)
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()
    # The trigger refuses raw SQL too, not only the ORM.
    with pytest.raises(IntegrityError):
        session.connection().execute(text("UPDATE token SET secret_hash = 'raw'"))
    session.rollback()
    kept = _tokens(session)[0]
    assert (kept.expires_at, kept.secret_hash, kept.user_id) == (
        original.expires_at,
        original.secret_hash,
        original.user_id,
    )
    assert authenticate(session, secret).id == original.user_id


def test_authenticate_refuses_unknown_and_expired_secrets(session):
    create_account(session, "Ada", "ada@example.com", "password-1")
    secret = login(session, "ada@example.com", "password-1", session_length=SESSION_LENGTH)
    with pytest.raises(ServiceError):
        authenticate(session, "missing")
    with pytest.raises(ServiceError):
        authenticate(session, _issued_key(session, secret))
    token = _tokens(session)[0]
    set_token_expiry(session, token.user_id, (datetime.now(UTC) - timedelta(seconds=1)).isoformat())
    with pytest.raises(ServiceError):
        authenticate(session, secret)
    assert len(_tokens(session)) == 1
    login(session, "ada@example.com", "password-1", session_length=SESSION_LENGTH)
    assert len(_tokens(session)) == 1
    with pytest.raises(ServiceError):
        authenticate(session, secret)


def test_authenticate_refuses_a_session_whose_user_is_gone(session):
    human = create_account(session, "Ada", "ada@example.com", "password-1")
    secret = login(session, "ada@example.com", "password-1", session_length=SESSION_LENGTH)
    session.commit()
    # Foreign keys only switch off outside a transaction, so use a bare connection.
    raw = sqlite3.connect(session.get_bind().url.database)
    raw.execute("PRAGMA foreign_keys=OFF")
    raw.execute("DELETE FROM user WHERE id = ?", (human.user_id,))
    raw.commit()
    raw.close()
    # End the read transaction that loaded human.user_id, so the delete is visible.
    session.rollback()
    session.expunge_all()
    with pytest.raises(ServiceError):
        authenticate(session, secret)
    assert len(_tokens(session)) == 1


def test_a_dead_agent_key_deletes_that_session_only(session):
    human, secret = make_account(session)
    agent, issued = live_agent(session, secret)
    agent_secret = exchange_key(session, issued.key, session_length=SESSION_LENGTH)
    update_agent(session, agent.user_id, key_hash=None, key_expires_at=None)
    session.commit()
    with pytest.raises(ServiceError):
        authenticate(session, agent_secret)
    assert session.exec(select(Token).where(Token.user_id == agent.user_id)).first() is None
    assert authenticate(session, secret).id == human.user_id


def test_dead_agent_key_stays_deleted_when_the_request_rolls_back(database):
    SQLModel.metadata.create_all(db.get_engine())
    with db.open_session() as db_session:
        _human, secret = make_account(db_session)
        agent, issued = live_agent(db_session, secret)
        agent_secret = exchange_key(db_session, issued.key, session_length=SESSION_LENGTH)
        agent_id = agent.user_id
    with db.open_session() as db_session:
        update_agent(
            db_session,
            agent_id,
            key_expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        )
    with pytest.raises(ServiceError), db.open_session() as db_session:
        db_session.add(User(name="Unsaved"))
        db_session.flush()
        authenticate(db_session, agent_secret)
    with db.open_session() as db_session:
        assert db_session.exec(select(Token).where(Token.user_id == agent_id)).first() is None
        assert db_session.exec(select(User).where(User.name == "Unsaved")).first() is None
        assert authenticate(db_session, secret).name == "Ada"


def test_dead_agent_key_delete_survives_a_session_closed_without_commit(session):
    _human, secret = make_account(session)
    agent, issued = live_agent(session, secret)
    agent_secret = exchange_key(session, issued.key, session_length=SESSION_LENGTH)
    update_agent(session, agent.user_id, key_hash=None, key_expires_at=None)
    session.commit()
    request = Session(session.get_bind())
    request.add(User(name="Unsaved"))
    with pytest.raises(ServiceError):
        authenticate(request, agent_secret)
    request.close()
    assert session.exec(select(Token).where(Token.user_id == agent.user_id)).first() is None
    assert session.exec(select(User).where(User.name == "Unsaved")).first() is None


def test_dead_agent_key_delete_commits_with_the_caller(session):
    _human, secret = make_account(session)
    agent, issued = live_agent(session, secret)
    agent_secret = exchange_key(session, issued.key, session_length=SESSION_LENGTH)
    update_agent(session, agent.user_id, key_hash=None, key_expires_at=None)
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
            exchange_key(session, secret, session_length=SESSION_LENGTH)
        with pytest.raises(ServiceError):
            authenticate(session, secret)
        logout(session, secret)
    assert _tokens(session) == []


def test_a_new_session_without_expiry_is_refused(session):
    human = create_account(session, "Ada", "ada@example.com", "password-1")
    session.add(Token(user_id=human.user_id, secret_hash="hash"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_a_naive_expiry_is_still_compared_in_utc(session):
    create_account(session, "Ada", "ada@example.com", "password-1")
    secret = login(session, "ada@example.com", "password-1", session_length=SESSION_LENGTH)
    token = _tokens(session)[0]
    set_token_expiry(session, token.user_id, "2000-01-01 00:00:00")
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
    secret = login(session, "ada@example.com", "password-1", session_length=SESSION_LENGTH)
    logout(session, secret)
    assert _tokens(session) == []
    with pytest.raises(ServiceError):
        authenticate(session, secret)
    logout(session, secret)
    assert _tokens(session) == []


def test_overlapping_mints_leave_one_session(database):
    SQLModel.metadata.create_all(db.get_engine())
    with db.open_session() as db_session:
        human = create_account(db_session, "Ada", "ada@example.com", "password-1")
        user_id = human.user_id
    start = threading.Event()
    found: list[str] = []
    errors: list[BaseException] = []

    def mint() -> None:
        start.wait(timeout=5)
        try:
            with db.open_session() as db_session:
                found.append(auth._mint(db_session, user_id, SESSION_LENGTH))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=mint), threading.Thread(target=mint)]
    for thread in threads:
        thread.start()
    start.set()
    for thread in threads:
        thread.join(timeout=10)
    # The second mint waits for the first's lock, then replaces its row.
    assert errors == []
    assert len(found) == 2
    with db.open_session() as db_session:
        rows = list(db_session.exec(select(Token)).all())
        assert len(rows) == 1
        # found is in commit order, so the later mint's secret is the live one.
        assert authenticate(db_session, found[1]).id == user_id
        with pytest.raises(ServiceError):
            authenticate(db_session, found[0])


def _issued_key(session: Session, secret: str) -> str:
    _agent, issued = live_agent(session, secret, "Keyed")
    return issued.key
