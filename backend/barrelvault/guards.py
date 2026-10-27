"""Write rules checked before every flush of every session."""

from sqlalchemy import event, inspect, select
from sqlalchemy.orm import Session

from barrelvault.errors import ServiceError
from barrelvault.models import Agent, Human, Token, User

# Set in session.info by the service while it issues, rolls, or retires a key.
KEY_WRITE = "agent_key_write"
# Set in session.info by authenticate: the acting user's id and "human" or "agent".
ACTOR = "actor"

_ACCOUNT_ROWS = (User, Human, Agent)


@event.listens_for(Session, "before_flush")
def _check_flush(session: Session, _flush_context: object, _instances: object) -> None:
    actor = session.info.get(ACTOR)
    for obj in session.new:
        _check_actor(actor, obj)
        if isinstance(obj, Token):
            _check_new_token(obj)
        elif isinstance(obj, Human):
            if actor is not None:
                raise ServiceError("refused")
            _check_new_profile(session, obj, Agent)
        elif isinstance(obj, Agent):
            _check_new_profile(session, obj, Human)
            _check_owner(session, obj, actor)
            _check_key_pair(obj)
    for obj in session.dirty:
        if not session.is_modified(obj):
            continue
        _check_actor(actor, obj)
        if isinstance(obj, Token):
            _check_token_update(obj)
        elif isinstance(obj, Agent):
            _check_agent_update(obj, session.info.get(KEY_WRITE))
    for obj in session.deleted:
        _check_actor(actor, obj)


def _check_actor(actor: tuple[int, str] | None, obj: object) -> None:
    """An agent session cannot write a User, a Human, or an Agent."""
    if actor is not None and actor[1] == "agent" and isinstance(obj, _ACCOUNT_ROWS):
        raise ServiceError("refused")


def _check_new_profile(session: Session, profile: Human | Agent, other: type) -> None:
    """A user keeps the one profile it has. The stored row counts until it is deleted."""
    if any(isinstance(obj, other) and obj.user_id == profile.user_id for obj in session.new):
        raise ServiceError("refused")
    if _stored(session, other, profile.user_id):
        raise ServiceError("refused")


def _check_owner(session: Session, agent: Agent, actor: tuple[int, str] | None) -> None:
    if actor is not None and agent.owner_id != actor[0]:
        raise ServiceError("refused")
    pending = any(
        isinstance(obj, Human) and obj.user_id == agent.owner_id for obj in session.new
    )
    if not pending and not _stored(session, Human, agent.owner_id):
        raise ServiceError("refused")


def _stored(session: Session, model: type, user_id: int | None) -> bool:
    row = session.connection().execute(
        select(model.user_id).where(model.user_id == user_id)
    ).first()
    return row is not None


def _check_new_token(token: Token) -> None:
    if token.expires_at is None:
        raise ServiceError("refused")


def _check_token_update(token: Token) -> None:
    _check_new_token(token)
    state = inspect(token)
    for name in ("expires_at", "secret_hash", "user_id"):
        if state.attrs[name].history.has_changes():
            raise ServiceError("refused")


def _check_agent_update(agent: Agent, key_write: object) -> None:
    _check_key_pair(agent)
    state = inspect(agent)
    if state.attrs.owner_id.history.has_changes():
        raise ServiceError("refused")
    key_changed = any(
        state.attrs[name].history.has_changes() for name in ("key_hash", "key_expires_at")
    )
    if key_changed and key_write not in ("issue", "roll", "retire"):
        raise ServiceError("refused")


def _check_key_pair(agent: Agent) -> None:
    if (agent.key_hash is None) != (agent.key_expires_at is None):
        raise ServiceError("refused")
