"""Write rules that depend on which call is writing, checked before every flush.

Rules that hold for every write are constraints and triggers in models.py.
"""

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

from barrelvault.errors import Forbidden
from barrelvault.models import Agent, Human

# Set in session.info by the service while it issues, rolls, or retires a key.
KEY_WRITE = "agent_key_write"
# Set in session.info by setup while it writes the first human. No other write adds one.
SETUP_WRITE = "setup_write"


@event.listens_for(Session, "before_flush")
def _check_flush(session: Session, _flush_context: object, _instances: object) -> None:
    for obj in session.new:
        if isinstance(obj, Human) and not session.info.get(SETUP_WRITE):
            raise Forbidden("only setup creates a human")
    for obj in session.dirty:
        if isinstance(obj, Agent) and session.is_modified(obj):
            _check_agent_update(obj, session.info.get(KEY_WRITE))


def _check_agent_update(agent: Agent, key_write: object) -> None:
    state = inspect(agent)
    key_changed = any(
        state.attrs[name].history.has_changes() for name in ("key_hash", "key_expires_at")
    )
    if key_changed and key_write not in ("issue", "roll", "retire"):
        raise Forbidden("an agent's key changes only by issue, roll, or retire")
    if state.attrs.retired_at.history.has_changes() and key_write != "retire":
        raise Forbidden("an agent is retired only by retire")
