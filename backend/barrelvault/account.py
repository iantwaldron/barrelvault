"""Account and agent writes. Each call takes the acting user; none reads a session."""

from datetime import UTC, datetime
from typing import NamedTuple

from sqlalchemy import or_, text
from sqlmodel import Session, col, select

from barrelvault.auth import delete_session, hash_password, normalize_email
from barrelvault.errors import Conflict, Forbidden, Invalid, NotFound
from barrelvault.guards import KEY_WRITE, SETUP_WRITE
from barrelvault.models import Agent, Human, User
from barrelvault.times import check_timezone

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


def create_account(session: Session, name: str, email: str, password: str, timezone: str) -> Human:
    """Create the first human. Setup is the only caller."""
    fields = (("name", name), ("email", email), ("timezone", timezone), ("password", password))
    for field, value in fields:
        if _blank(value):
            raise Invalid(f"{field} is required")
    address = normalize_email(email)
    check_timezone(timezone)
    if is_set_up(session):
        raise Conflict("setup has already run")
    human = Human(
        user_id=_add_user(session, name),
        email=address,
        password_hash=hash_password(password),
        timezone=timezone,
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
    if _blank(name):
        raise Invalid("name is required")
    if _name_taken(session, human, name):
        raise Conflict("name is taken")
    agent = Agent(user_id=_add_user(session, name), owner_id=human.user_id)
    session.add(agent)
    session.flush()
    return agent


def issue_key(session: Session, actor: User, agent_user_id: int, *, key_length: int) -> IssuedKey:
    human = _acting_human(session, actor)
    agent = _agent(session, human, agent_user_id)
    if agent.retired_at is not None:
        raise Conflict("agent is retired")
    if agent.key_hash is not None:
        raise Conflict("agent already has a key")
    with _key_write(session, "issue"):
        raw = agent.generate_key(key_length)
        session.add(agent)
        session.flush()
    return _issued(raw, agent)


def roll_key(session: Session, actor: User, agent_user_id: int, *, key_length: int) -> IssuedKey:
    human = _acting_human(session, actor)
    agent = _agent(session, human, agent_user_id)
    if agent.retired_at is not None:
        raise Conflict("agent is retired")
    if agent.key_hash is None:
        raise Conflict("agent has no key")
    with _key_write(session, "roll"):
        raw = agent.generate_key(key_length)
        delete_session(session, agent.user_id)
        session.add(agent)
        session.flush()
    return _issued(raw, agent)


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


def delete_agent(session: Session, actor: User, agent_user_id: int) -> bool:
    """Delete an agent with nothing attached and return True.

    With data attached, change nothing and return False, so the caller can ask
    the person to reassign or retire.
    """
    human = _acting_human(session, actor)
    agent = _agent(session, human, agent_user_id)
    if _attached(session, agent.user_id):
        return False
    _remove_agent(session, agent)
    return True


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


def _add_user(session: Session, name: str) -> int:
    user = User(name=name)
    session.add(user)
    session.flush()
    assert user.id is not None, "the flush assigns the id"
    return user.id


def _issued(raw: str, agent: Agent) -> IssuedKey:
    assert agent.key_expires_at is not None, "generate_key sets the expiry"
    return IssuedKey(raw, agent.key_expires_at)


def _acting_human(session: Session, actor: User) -> Human:
    """Only a person creates or changes an agent. An acting agent is refused."""
    human = session.get(Human, actor.id)
    if human is None:
        raise Forbidden("only a person changes agents")
    return human


def _agent(session: Session, human: Human, user_id: int) -> Agent:
    """Return an agent in this human's account. Any other agent is refused."""
    agent = session.exec(
        select(Agent).where(Agent.user_id == user_id, Agent.owner_id == human.user_id)
    ).one_or_none()
    if agent is None:
        raise NotFound("no such agent")
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
            row = (
                session.connection()
                .execute(
                    text(f"SELECT 1 FROM {table} WHERE {column} = :user_id LIMIT 1"),
                    {"user_id": user_id},
                )
                .first()
            )
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
                    f"UPDATE {table} SET {column} = :human_user_id WHERE {column} = :agent_user_id"
                ),
                {"human_user_id": human_user_id, "agent_user_id": agent_user_id},
            )


def _table_names(session: Session) -> set[str]:
    rows = session.connection().execute(text("SELECT name FROM sqlite_master WHERE type = 'table'"))
    return {str(row[0]) for row in rows}


def _name_taken(session: Session, human: Human, name: str) -> bool:
    """True when the person or one of their agents has this name, in any case."""
    in_account = or_(
        col(User.id) == human.user_id,
        col(User.id).in_(select(Agent.user_id).where(Agent.owner_id == human.user_id)),
    )
    same_name = col(User.name).collate("NOCASE") == name
    return session.exec(select(User).where(in_account, same_name)).first() is not None


def _blank(value: str | None) -> bool:
    return value is None or value.strip() == ""
