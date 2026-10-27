"""Write rules checked before every flush of every session."""

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

from barrelvault.errors import ServiceError
from barrelvault.models import Agent, Token

# Set in session.info by the service while it issues, rolls, or retires a key.
KEY_WRITE = "agent_key_write"


@event.listens_for(Session, "before_flush")
def _check_flush(session: Session, _flush_context: object, _instances: object) -> None:
    for obj in session.new:
        if isinstance(obj, Token):
            _check_new_token(obj)
        elif isinstance(obj, Agent):
            _check_key_pair(obj)
    for obj in session.dirty:
        if not session.is_modified(obj):
            continue
        if isinstance(obj, Token):
            _check_token_update(obj)
        elif isinstance(obj, Agent):
            _check_agent_update(obj, session.info.get(KEY_WRITE))


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
