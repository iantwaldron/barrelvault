"""Passwords, session minting, and the session check."""

import hashlib
from datetime import UTC, datetime

from email_validator import EmailNotValidError, validate_email
from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError
from sqlalchemy import delete, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import SessionTransaction
from sqlmodel import Session, col, select

from barrelvault.errors import Invalid, Refused
from barrelvault.models import Agent, Human, Token, User
from barrelvault.times import as_utc

_passwords = PasswordHash.recommended()
_DUMMY_PASSWORD_HASH = _passwords.hash("barrelvault-password-check")
# Session row ids that authenticate deleted for a dead agent key.
_DEAD_SESSIONS = "dead_sessions"


def hash_password(password: str) -> str:
    return _passwords.hash(password)


def normalize_email(email: str) -> str:
    """The form an address is stored and matched in: checked, normalized, lowercased.

    Only the syntax is checked, so this never reaches the network. A string that
    is not an address raises Invalid.
    """
    try:
        checked = validate_email(email.strip(), check_deliverability=False)
    except EmailNotValidError:
        raise Invalid("email is not a valid address") from None
    return checked.normalized.lower()


def check_password(session: Session, email: str, password: str) -> int:
    """The user id whose email and password these are. Login's first step, which only reads.

    The check is slow on purpose, so the caller runs it before taking the write lock.
    """
    human = _human_by_email(session, email)
    stored = human.password_hash if human is not None else _DUMMY_PASSWORD_HASH
    try:
        password_matches = _passwords.verify(password, stored)
    except UnknownHashError:
        password_matches = False
    if human is None or not password_matches:
        raise Refused("refused")
    return human.user_id


def exchange_key(session: Session, key: str, *, session_length: int) -> str:
    if _blank(key):
        raise Refused("refused")
    agent = session.exec(select(Agent).where(Agent.key_hash == _digest(key))).one_or_none()
    # A retired agent has no key, so it never matches here.
    if agent is None or _agent_key_is_dead(agent):
        raise Refused("refused")
    return start_session(session, agent.user_id, session_length=session_length)


def authenticate(session: Session, secret: str) -> User:
    """Accept a session secret and return its user. The API and MCP use only this check."""
    if _blank(secret):
        raise Refused("refused")
    token = session.exec(select(Token).where(Token.secret_hash == _digest(secret))).one_or_none()
    if token is None or as_utc(token.expires_at) <= datetime.now(UTC):
        raise Refused("refused")
    user = session.get_one(User, token.user_id)
    agent = session.get(Agent, user.id)
    if agent is not None and _agent_key_is_dead(agent):
        # The delete rides the caller's transaction. If that rolls back, the
        # hook below repeats only the delete, so the caller's writes stay undone.
        session.info.setdefault(_DEAD_SESSIONS, set()).add(token.id)
        session.delete(token)
        raise Refused("refused")
    return user


def _human_by_email(session: Session, email: str) -> Human | None:
    """A malformed address matches no one, and the caller still checks a password."""
    try:
        address = normalize_email(email)
    except Invalid:
        return None
    return session.exec(select(Human).where(Human.email == address)).one_or_none()


def session_expires_at(session: Session, secret: str) -> datetime:
    """When a session just minted expires. The exchange returns it with the token."""
    token = session.exec(select(Token).where(Token.secret_hash == _digest(secret))).one()
    return as_utc(token.expires_at)


def delete_session(session: Session, user_id: int) -> None:
    """End the user's session, if it has one. Logout, roll, retire, and removal call this."""
    token = session.exec(select(Token).where(Token.user_id == user_id)).one_or_none()
    if token is not None:
        session.delete(token)
        session.flush()


def start_session(session: Session, user_id: int, *, session_length: int) -> str:
    """Mint a session for the user, replacing any it had. Return the raw secret."""
    token, raw = Token.mint(user_id, session_length)
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
    bind = session.get_bind()
    engine = bind if isinstance(bind, Engine) else bind.engine
    with engine.begin() as connection:
        connection.execute(delete(Token).where(col(Token.id).in_(token_ids)))


def _agent_key_is_dead(agent: Agent) -> bool:
    """No key, which includes a retired agent, or a key past its expiry."""
    if agent.key_expires_at is None:
        return True
    return as_utc(agent.key_expires_at) <= datetime.now(UTC)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _blank(value: str | None) -> bool:
    return value is None or value.strip() == ""
