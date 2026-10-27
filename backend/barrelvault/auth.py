"""Passwords, session minting, and the session check."""

import hashlib
from datetime import UTC, datetime

from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError
from sqlalchemy import delete, event
from sqlalchemy.orm import Session as OrmSession, SessionTransaction
from sqlmodel import Session, select

from barrelvault.errors import ServiceError
from barrelvault.guards import ACTOR
from barrelvault.models import Agent, Human, Token, User

_passwords = PasswordHash.recommended()
_DUMMY_PASSWORD_HASH = _passwords.hash("barrelvault-password-check")
# Session row ids that authenticate deleted for a dead agent key.
_DEAD_SESSIONS = "dead_sessions"


def hash_password(password: str) -> str:
    return _passwords.hash(password)


def login(session: Session, email: str, password: str) -> str:
    human = session.exec(select(Human).where(Human.email == email)).one_or_none()
    stored = human.password_hash if human is not None else _DUMMY_PASSWORD_HASH
    try:
        password_matches = _passwords.verify(password, stored)
    except UnknownHashError:
        password_matches = False
    if human is None or not password_matches:
        raise ServiceError("refused")
    return _mint(session, human.user_id)


def exchange_key(session: Session, key: str) -> str:
    if _blank(key):
        raise ServiceError("refused")
    agent = session.exec(select(Agent).where(Agent.key_hash == _digest(key))).one_or_none()
    if agent is None or agent.retired_at is not None:
        raise ServiceError("refused")
    if agent.key_expires_at is None or _as_utc(agent.key_expires_at) <= datetime.now(UTC):
        raise ServiceError("refused")
    return _mint(session, agent.user_id)


def authenticate(session: Session, secret: str) -> User:
    """Accept a session secret. This is the only check later calls use."""
    session.info.pop(ACTOR, None)
    if _blank(secret):
        raise ServiceError("refused")
    token = session.exec(select(Token).where(Token.secret_hash == _digest(secret))).one_or_none()
    if token is None or _as_utc(token.expires_at) <= datetime.now(UTC):
        raise ServiceError("refused")
    user = session.get(User, token.user_id)
    if user is None:
        raise ServiceError("refused")
    agent = session.get(Agent, user.id)
    if agent is not None and _agent_key_is_dead(agent):
        # The delete rides the caller's transaction. If that rolls back, the
        # hook below repeats only the delete, so the caller's writes stay undone.
        session.info.setdefault(_DEAD_SESSIONS, set()).add(token.id)
        session.delete(token)
        raise ServiceError("refused")
    session.info[ACTOR] = (user.id, "agent" if agent is not None else "human")
    return user


def logout(session: Session, secret: str) -> None:
    if _blank(secret):
        return
    token = session.exec(select(Token).where(Token.secret_hash == _digest(secret))).one_or_none()
    if token is not None:
        session.delete(token)
        session.flush()


def delete_session(session: Session, user_id: int) -> None:
    token = session.exec(select(Token).where(Token.user_id == user_id)).one_or_none()
    if token is not None:
        session.delete(token)


def _mint(session: Session, user_id: int) -> str:
    token = Token(user_id=user_id)
    raw = token.generate_secret()
    existing = session.exec(select(Token).where(Token.user_id == user_id)).one_or_none()
    if existing is not None:
        session.delete(existing)
        session.flush()
    session.add(token)
    session.flush()
    return raw


@event.listens_for(OrmSession, "after_commit")
def _forget_dead_sessions(session: OrmSession) -> None:
    session.info.pop(_DEAD_SESSIONS, None)


@event.listens_for(OrmSession, "after_transaction_end")
def _delete_dead_sessions(session: OrmSession, transaction: SessionTransaction) -> None:
    if transaction.parent is not None:
        return
    token_ids = session.info.pop(_DEAD_SESSIONS, None)
    if not token_ids:
        return
    with session.get_bind().begin() as connection:
        connection.execute(delete(Token).where(Token.id.in_(token_ids)))


def _agent_key_is_dead(agent: Agent) -> bool:
    if agent.retired_at is not None:
        return True
    if agent.key_hash is None or agent.key_expires_at is None:
        return True
    return _as_utc(agent.key_expires_at) <= datetime.now(UTC)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _blank(value: str | None) -> bool:
    return value is None or value.strip() == ""


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
