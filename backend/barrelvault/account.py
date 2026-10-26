"""Account and agent writes."""

from datetime import UTC, datetime
from typing import NamedTuple

from sqlalchemy import event, inspect, text
from sqlalchemy.orm import Session as OrmSession
from sqlmodel import Session, select

from barrelvault.auth import (
    ServiceError,
    authenticate,
    delete_session,
    hash_password,
    install_guard,
)
from barrelvault.models import Agent, Human, User
from barrelvault.settings import load_settings

_REFERENCES = (
    ("task", ("created_by", "updated_by", "owned_by")),
    ("block", ("created_by", "released_by")),
    ("task_history", ("user_id",)),
)


class IssuedKey(NamedTuple):
    key: str
    key_expires_at: datetime


def create_account(
    session: Session,
    name: str,
    email: str,
    password: str,
    *,
    secret: str | None = None,
) -> Human:
    """Create the one human account. A session secret is refused."""
    _prepare(session)
    if secret is not None:
        raise ServiceError("refused")
    if _blank(name) or _blank(email) or _blank(password):
        raise ServiceError("refused")
    if session.exec(select(Human)).first() is not None:
        raise ServiceError("refused")
    if _name_taken(session, name):
        raise ServiceError("refused")
    user = User(name=name)
    session.add(user)
    session.flush()
    human = Human(
        user_id=user.id,
        email=email,
        password_hash=hash_password(password),
    )
    session.add(human)
    session.flush()
    return human


def add_agent(session: Session, secret: str, name: str) -> Agent:
    human = _human_actor(session, secret)
    if _blank(name) or _name_taken(session, name):
        raise ServiceError("refused")
    user = User(name=name)
    session.add(user)
    session.flush()
    agent = Agent(user_id=user.id, owner_id=human.user_id)
    _check_owner(agent, human)
    _check_key_pair(agent)
    session.add(agent)
    session.flush()
    return agent


def issue_key(session: Session, secret: str, agent_user_id: int) -> IssuedKey:
    _human_actor(session, secret)
    load_settings()
    agent = _agent(session, agent_user_id)
    if agent.retired_at is not None or agent.key_hash is not None:
        raise ServiceError("refused")
    with _key_write(session, "issue"):
        raw = agent.generate_key()
        _check_key_pair(agent)
        session.add(agent)
        session.flush()
    return IssuedKey(raw, agent.key_expires_at)


def roll_key(session: Session, secret: str, agent_user_id: int) -> IssuedKey:
    _human_actor(session, secret)
    load_settings()
    agent = _agent(session, agent_user_id)
    if agent.retired_at is not None or agent.key_hash is None or agent.key_expires_at is None:
        raise ServiceError("refused")
    with _key_write(session, "roll"):
        raw = agent.generate_key()
        _check_key_pair(agent)
        delete_session(session, agent.user_id)
        session.add(agent)
        session.flush()
    return IssuedKey(raw, agent.key_expires_at)


def retire_agent(session: Session, secret: str, agent_user_id: int) -> None:
    _human_actor(session, secret)
    agent = _agent(session, agent_user_id)
    with _key_write(session, "retire"):
        if agent.retired_at is None:
            agent.retired_at = datetime.now(UTC)
        agent.key_hash = None
        agent.key_expires_at = None
        _check_key_pair(agent)
        delete_session(session, agent.user_id)
        session.add(agent)
        session.flush()


def delete_agent(session: Session, secret: str, agent_user_id: int) -> None:
    _human_actor(session, secret)
    agent = _agent(session, agent_user_id)
    if _attached(session, agent.user_id):
        return
    _remove_agent(session, agent)


def reassign_agent(session: Session, secret: str, agent_user_id: int) -> None:
    human = _human_actor(session, secret)
    agent = _agent(session, agent_user_id)
    _move_references(session, agent.user_id, human.user_id)
    _remove_agent(session, agent)


def add_profile(session: Session, secret: str, user_id: int, kind: str) -> None:
    """Refuse a second profile on a user that already has one."""
    _human_actor(session, secret)
    _refuse_profile_change(session, user_id, kind)


def replace_profile(session: Session, secret: str, user_id: int, kind: str) -> None:
    """Refuse swapping a human profile and an agent profile."""
    _human_actor(session, secret)
    _refuse_profile_change(session, user_id, kind)


def assign_owner(session: Session, secret: str, agent_user_id: int, owner_id: int) -> None:
    human = _human_actor(session, secret)
    agent = _agent(session, agent_user_id)
    if owner_id != human.user_id or owner_id != agent.owner_id:
        raise ServiceError("refused")


def save_agent(session: Session, secret: str, agent: Agent) -> None:
    """Flush an existing agent. Owner, key, and expiry edits are refused."""
    _human_actor(session, secret)
    if not inspect(agent).persistent:
        raise ServiceError("refused")
    session.flush()


def _prepare(session: Session) -> None:
    install_guard(session)
    _install_agent_guard(session)


def _install_agent_guard(session: Session) -> None:
    if session.info.get("agent_guard"):
        return
    session.info["agent_guard"] = True
    event.listen(session, "before_flush", _guard_flush)


def _guard_flush(session: OrmSession, _flush_context: object, _instances: object) -> None:
    allowed = session.info.get("agent_key_write")
    for obj in session.new:
        if isinstance(obj, Agent):
            _check_key_pair(obj)
    for obj in session.dirty:
        if isinstance(obj, Agent):
            _guard_agent(obj, allowed)


def _guard_agent(agent: Agent, allowed: object) -> None:
    _check_key_pair(agent)
    state = inspect(agent)
    if not state.persistent:
        return
    if state.attrs.owner_id.history.has_changes():
        raise ServiceError("refused")
    key_changed = any(
        state.attrs[name].history.has_changes() for name in ("key_hash", "key_expires_at")
    )
    if key_changed and allowed not in ("issue", "roll", "retire"):
        raise ServiceError("refused")


class _key_write:
    def __init__(self, session: Session, kind: str) -> None:
        self.session = session
        self.kind = kind

    def __enter__(self) -> None:
        _install_agent_guard(self.session)
        self.session.info["agent_key_write"] = self.kind

    def __exit__(self, *_exc: object) -> None:
        self.session.info.pop("agent_key_write", None)


def _human_actor(session: Session, secret: str) -> Human:
    _prepare(session)
    user = authenticate(session, secret)
    human = session.get(Human, user.id)
    if human is None:
        raise ServiceError("refused")
    return human


def _agent(session: Session, user_id: int) -> Agent:
    agent = session.get(Agent, user_id)
    if agent is None:
        raise ServiceError("refused")
    return agent


def _remove_agent(session: Session, agent: Agent) -> None:
    user_id = agent.user_id
    delete_session(session, user_id)
    user = session.get(User, user_id)
    session.delete(agent)
    if user is not None:
        session.delete(user)
    session.flush()


def _refuse_profile_change(session: Session, user_id: int, kind: str) -> None:
    human = session.get(Human, user_id)
    agent = session.get(Agent, user_id)
    if kind not in ("human", "agent"):
        raise ServiceError("refused")
    if human is not None or agent is not None:
        raise ServiceError("refused")
    raise ServiceError("refused")


def _check_owner(agent: Agent, human: Human) -> None:
    if agent.owner_id != human.user_id:
        raise ServiceError("refused")


def _check_key_pair(agent: Agent) -> None:
    if (agent.key_hash is None) != (agent.key_expires_at is None):
        raise ServiceError("refused")


def _attached(session: Session, user_id: int) -> bool:
    names = _table_names(session)
    for table, columns in _REFERENCES:
        if table not in names:
            continue
        for column in columns:
            row = session.connection().execute(
                text(f"SELECT 1 FROM {table} WHERE {column} = :user_id LIMIT 1"),
                {"user_id": user_id},
            ).first()
            if row is not None:
                return True
    return False


def _move_references(session: Session, agent_user_id: int, human_user_id: int) -> None:
    names = _table_names(session)
    for table, columns in _REFERENCES:
        if table not in names:
            continue
        for column in columns:
            if column == "owned_by":
                continue
            session.connection().execute(
                text(
                    f"UPDATE {table} SET {column} = :human_user_id "
                    f"WHERE {column} = :agent_user_id"
                ),
                {"human_user_id": human_user_id, "agent_user_id": agent_user_id},
            )


def _table_names(session: Session) -> set[str]:
    rows = session.connection().execute(
        text("SELECT name FROM sqlite_master WHERE type = 'table'")
    )
    return {str(row[0]) for row in rows}


def _name_taken(session: Session, name: str) -> bool:
    return session.exec(select(User).where(User.name == name)).first() is not None


def _blank(value: str | None) -> bool:
    return value is None or value.strip() == ""
