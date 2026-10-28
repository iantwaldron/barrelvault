"""Account and agent writes. Each call takes the acting user; none reads a session."""

from datetime import UTC, datetime
from typing import NamedTuple

from sqlalchemy import text
from sqlmodel import Session, select

from barrelvault.auth import ServiceError, delete_session, hash_password
from barrelvault.guards import KEY_WRITE, SETUP_WRITE
from barrelvault.models import Agent, Human, User

_REFERENCES = (
    ("task", ("created_by", "updated_by", "owned_by")),
    ("block", ("created_by", "released_by")),
    ("task_history", ("user_id",)),
)


class IssuedKey(NamedTuple):
    key: str
    key_expires_at: datetime


def is_set_up(session: Session) -> bool:
    """Any user row means setup has already run."""
    return session.exec(select(User)).first() is not None


def create_account(session: Session, name: str, email: str, password: str) -> Human:
    """Create the first human. Setup is the only caller."""
    if _blank(name) or _blank(email) or _blank(password):
        raise ServiceError("refused")
    if is_set_up(session):
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
    session.info[SETUP_WRITE] = True
    try:
        session.flush()
    finally:
        session.info.pop(SETUP_WRITE, None)
    return human


def add_agent(session: Session, actor: User, name: str) -> Agent:
    human = _acting_human(session, actor)
    if _blank(name) or _name_taken(session, name):
        raise ServiceError("refused")
    user = User(name=name)
    session.add(user)
    session.flush()
    agent = Agent(user_id=user.id, owner_id=human.user_id)
    session.add(agent)
    session.flush()
    return agent


def issue_key(session: Session, actor: User, agent_user_id: int, *, key_length: int) -> IssuedKey:
    human = _acting_human(session, actor)
    agent = _agent(session, human, agent_user_id)
    if agent.retired_at is not None or agent.key_hash is not None:
        raise ServiceError("refused")
    with _key_write(session, "issue"):
        raw = agent.generate_key(key_length)
        session.add(agent)
        session.flush()
    return IssuedKey(raw, agent.key_expires_at)


def roll_key(session: Session, actor: User, agent_user_id: int, *, key_length: int) -> IssuedKey:
    human = _acting_human(session, actor)
    agent = _agent(session, human, agent_user_id)
    if agent.retired_at is not None or agent.key_hash is None or agent.key_expires_at is None:
        raise ServiceError("refused")
    with _key_write(session, "roll"):
        raw = agent.generate_key(key_length)
        delete_session(session, agent.user_id)
        session.add(agent)
        session.flush()
    return IssuedKey(raw, agent.key_expires_at)


def retire_agent(session: Session, actor: User, agent_user_id: int) -> None:
    human = _acting_human(session, actor)
    agent = _agent(session, human, agent_user_id)
    with _key_write(session, "retire"):
        if agent.retired_at is None:
            agent.retired_at = datetime.now(UTC)
        agent.key_hash = None
        agent.key_expires_at = None
        delete_session(session, agent.user_id)
        session.add(agent)
        session.flush()


def delete_agent(session: Session, actor: User, agent_user_id: int) -> None:
    human = _acting_human(session, actor)
    agent = _agent(session, human, agent_user_id)
    if _attached(session, agent.user_id):
        return
    _remove_agent(session, agent)


def reassign_agent(session: Session, actor: User, agent_user_id: int) -> None:
    human = _acting_human(session, actor)
    agent = _agent(session, human, agent_user_id)
    _move_references(session, agent.user_id, human.user_id)
    _remove_agent(session, agent)


class _key_write:
    def __init__(self, session: Session, kind: str) -> None:
        self.session = session
        self.kind = kind

    def __enter__(self) -> None:
        self.session.info[KEY_WRITE] = self.kind

    def __exit__(self, *_exc: object) -> None:
        self.session.info.pop(KEY_WRITE, None)


def _acting_human(session: Session, actor: User) -> Human:
    """Only a person creates or changes an agent. An acting agent is refused."""
    human = session.get(Human, actor.id)
    if human is None:
        raise ServiceError("refused")
    return human


def _agent(session: Session, human: Human, user_id: int) -> Agent:
    """Return an agent in this human's account. Any other agent is refused."""
    agent = session.exec(
        select(Agent).where(Agent.user_id == user_id, Agent.owner_id == human.user_id)
    ).one_or_none()
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
