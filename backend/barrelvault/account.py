"""The account and its agents. Each call takes the acting user; none reads a session."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, NamedTuple

from sqlalchemy import or_, update
from sqlmodel import Session, col, select

from barrelvault.auth import delete_session, hash_password, normalize_email
from barrelvault.errors import Conflict, Forbidden, Invalid, NotFound
from barrelvault.guards import KEY_WRITE, SETUP_WRITE
from barrelvault.models import Agent, Block, Human, Task, TaskHistory, User
from barrelvault.times import as_utc, check_timezone, now

Kind = Literal["person", "agent"]
PERSON: Kind = "person"
AGENT: Kind = "agent"

# Every column that names who did something. Reassign moves each one to the person.
# owned_by is left out: it is always the person already.
_ACTOR_COLUMNS = (
    (Task, ("created_by", "updated_by")),
    (Block, ("created_by", "released_by")),
    (TaskHistory, ("user_id",)),
)


class IssuedKey(NamedTuple):
    key: str
    key_expires_at: datetime


@dataclass(frozen=True)
class UserRef:
    """How a response names a user: never by id."""

    name: str
    kind: Kind


@dataclass(frozen=True)
class AccountView:
    """The acting user, and the account they act in."""

    name: str
    kind: Kind
    email: str
    timezone: str


@dataclass(frozen=True)
class AgentView:
    name: str
    retired_at: datetime | None
    key_expires_at: datetime | None


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
        user_id=_add_user(session, name.strip()),
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
    name = name.strip()
    # The API addresses an agent by its name as one path segment.
    if "/" in name:
        raise Invalid("name cannot contain /")
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
            agent.retired_at = now()
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


def account_view(session: Session, actor: User) -> AccountView:
    owner = account_of(session, actor)
    kind = PERSON if owner.user_id == actor.id else AGENT
    return AccountView(actor.name, kind, owner.email, owner.timezone)


def set_timezone(session: Session, actor: User, timezone: str) -> AccountView:
    """Change the account's timezone. Only the person changes it."""
    human = _acting_human(session, actor, "only the person changes the timezone")
    human.timezone = check_timezone(timezone)
    session.add(human)
    session.flush()
    return account_view(session, actor)


def list_agents(session: Session, actor: User) -> list[AgentView]:
    """The person's agents, retired ones included, by name."""
    human = _acting_human(session, actor)
    agents = {agent.user_id: agent for agent in human.get_agents(session)}
    users = session.exec(select(User).where(col(User.id).in_(agents)).order_by(col(User.name)))
    views = []
    for user in users:
        agent = agents[_id(user)]
        retired = as_utc(agent.retired_at) if agent.retired_at else None
        expires = as_utc(agent.key_expires_at) if agent.key_expires_at else None
        views.append(AgentView(user.name, retired, expires))
    return views


def agent_by_name(session: Session, actor: User, name: str) -> int:
    """The user id of the person's agent with this name, in any case.

    Another account's is not found.
    """
    human = _acting_human(session, actor)
    found = session.exec(
        select(Agent.user_id)
        .join(User, col(User.id) == Agent.user_id)
        .where(Agent.owner_id == human.user_id, col(User.name).collate("NOCASE") == name)
    ).first()
    if found is None:
        raise NotFound("no such agent")
    return found


def user_refs(session: Session, user_ids: set[int]) -> dict[int, UserRef]:
    """Name each user, as a person or an agent, for a response."""
    if not user_ids:
        return {}
    agents = set(session.exec(select(Agent.user_id).where(col(Agent.user_id).in_(user_ids))))
    users = session.exec(select(User).where(col(User.id).in_(user_ids)))
    return {_id(user): UserRef(user.name, AGENT if user.id in agents else PERSON) for user in users}


def actor_id(actor: User) -> int:
    """The acting user's id. A stored user always has one."""
    assert actor.id is not None, "a stored user has an id"
    return actor.id


def account_of(session: Session, actor: User) -> Human:
    """The person whose account the actor acts in: the actor, or an agent's owner."""
    human = session.get(Human, actor.id)
    if human is not None:
        return human
    agent = session.get(Agent, actor.id)
    owner = session.get(Human, agent.owner_id) if agent is not None else None
    if owner is None:
        raise NotFound("no such user")
    return owner


def _add_user(session: Session, name: str) -> int:
    user = User(name=name)
    session.add(user)
    session.flush()
    assert user.id is not None, "the flush assigns the id"
    return user.id


def _issued(raw: str, agent: Agent) -> IssuedKey:
    assert agent.key_expires_at is not None, "generate_key sets the expiry"
    return IssuedKey(raw, agent.key_expires_at)


def _acting_human(
    session: Session, actor: User, refusal: str = "only a person changes agents"
) -> Human:
    """The acting person. An acting agent is refused."""
    human = session.get(Human, actor.id)
    if human is None:
        raise Forbidden(refusal)
    return human


def _id(user: User) -> int:
    assert user.id is not None, "a stored user has an id"
    return user.id


def _agent(session: Session, human: Human, user_id: int) -> Agent:
    """Return an agent in this human's account. Any other agent is not found."""
    agent = session.exec(
        select(Agent).where(Agent.user_id == user_id, Agent.owner_id == human.user_id)
    ).one_or_none()
    if agent is None:
        raise NotFound("no such agent")
    return agent


def _remove_agent(session: Session, agent: Agent) -> None:
    user_id = agent.user_id
    delete_session(session, user_id)
    user = session.get_one(User, user_id)
    session.delete(agent)
    session.delete(user)
    session.flush()


def _attached(session: Session, user_id: int) -> bool:
    """True when a task, block, or history row names the user."""
    for model, columns in _ACTOR_COLUMNS:
        named = or_(*(getattr(model, column) == user_id for column in columns))
        if session.exec(select(model).where(named).limit(1)).first() is not None:
            return True
    return False


def _move_references(session: Session, agent_user_id: int, human_user_id: int) -> None:
    for model, columns in _ACTOR_COLUMNS:
        for column in columns:
            session.execute(
                update(model)
                .where(getattr(model, column) == agent_user_id)
                .values({column: human_user_id})
            )


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
