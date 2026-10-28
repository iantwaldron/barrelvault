"""User, Human, Agent, Token, Task, Block, and TaskHistory tables.

A rule that holds for every write, whoever makes it, is a constraint or a
trigger here, so a bulk statement or raw SQL cannot skip it. A rule that
depends on which call is writing lives in guards.py.
"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import DDL, CheckConstraint, Index, SmallInteger, event
from sqlmodel import Field, Session, SQLModel, select

from barrelvault.history import HistoryBase, history
from barrelvault.status import CLOSED
from barrelvault.times import as_utc


class User(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    # Unique within the account, without regard to case. The name_is_free triggers hold it.
    name: str = Field(index=True)


class Human(SQLModel, table=True):
    # auth.normalize_email writes every address. This holds the lowercase part of it.
    __table_args__ = (CheckConstraint("email = lower(email)", name="ck_human_email_lowercase"),)

    user_id: int = Field(foreign_key="user.id", primary_key=True)
    email: str = Field(unique=True)
    password_hash: str
    # An IANA name. An agent works in its owner's timezone.
    timezone: str

    def get_agents(self, session: Session) -> list["Agent"]:
        """Return the agents this human owns, including retired agents."""
        rows = session.exec(select(Agent).where(Agent.owner_id == self.user_id)).all()
        return list(rows)


class Agent(SQLModel, table=True):
    __table_args__ = (
        CheckConstraint("(key_hash IS NULL) = (key_expires_at IS NULL)", name="ck_agent_key_pair"),
        CheckConstraint(
            "retired_at IS NULL OR key_hash IS NULL", name="ck_agent_retired_without_key"
        ),
    )

    user_id: int = Field(foreign_key="user.id", primary_key=True)
    owner_id: int = Field(foreign_key="user.id", index=True)
    key_hash: str | None = Field(default=None, unique=True)
    key_expires_at: datetime | None = Field(default=None)
    retired_at: datetime | None = Field(default=None)

    def generate_key(self, key_length: int) -> str:
        """Store a new key hash and an expiry key_length seconds out. Return the raw key."""
        raw = secrets.token_hex(32)
        self.key_hash = hashlib.sha256(raw.encode()).hexdigest()
        self.key_expires_at = datetime.now(UTC) + timedelta(seconds=key_length)
        return raw


class Token(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", unique=True)
    secret_hash: str = Field(unique=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime

    @classmethod
    def mint(cls, user_id: int, session_length: int) -> tuple["Token", str]:
        """Build a session row with a new secret, expiring session_length seconds out.

        Return the row and the raw secret. Only the secret's hash is stored.
        """
        raw = secrets.token_hex(32)
        minted = datetime.now(UTC)
        token = cls(
            user_id=user_id,
            secret_hash=hashlib.sha256(raw.encode()).hexdigest(),
            created_at=minted,
            expires_at=minted + timedelta(seconds=session_length),
        )
        return token, raw


class TaskHistory(HistoryBase, table=True):
    __tablename__ = "task_history"
    __table_args__ = (Index("ix_task_history_task_id_created_at", "task_id", "created_at"),)

    task_id: int = Field(foreign_key="task.id", ondelete="CASCADE")


@history(TaskHistory)
class Task(SQLModel, table=True):
    __table_args__ = (
        CheckConstraint(
            "typeof(status) = 'integer' AND status IN (0, 1, 2, 3)", name="ck_task_status"
        ),
        CheckConstraint(
            "estimate_minutes IS NULL OR (typeof(estimate_minutes) = 'integer' "
            "AND estimate_minutes BETWEEN 0 AND 32767)",
            name="ck_task_estimate_minutes",
        ),
        CheckConstraint(
            "trim(title, ' ' || char(9) || char(10) || char(13)) != ''", name="ck_task_title"
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    parent_id: int | None = Field(
        default=None, foreign_key="task.id", ondelete="CASCADE", index=True
    )
    title: str
    notes: str | None = Field(default=None)
    tags: str | None = Field(default=None)
    status: int = Field(default=0, sa_type=SmallInteger, sa_column_kwargs={"server_default": "0"})
    estimate_minutes: int | None = Field(default=None, sa_type=SmallInteger)
    due_at: datetime | None = Field(default=None)
    created_by: int = Field(foreign_key="user.id")
    created_at: datetime
    updated_at: datetime
    updated_by: int = Field(foreign_key="user.id")
    owned_by: int = Field(foreign_key="user.id", index=True)

    def open_for(self, now: datetime | None = None) -> timedelta:
        """How long the task has been open. A closed task stops at its last update."""
        end = as_utc(self.updated_at) if self.status in CLOSED else now or datetime.now(UTC)
        return end - as_utc(self.created_at)


class Block(SQLModel, table=True):
    __table_args__ = (
        CheckConstraint(
            "trim(reason, ' ' || char(9) || char(10) || char(13)) != '' "
            "AND instr(reason, char(10)) = 0 AND instr(reason, char(13)) = 0",
            name="ck_block_reason",
        ),
        CheckConstraint(
            "(released_at IS NULL) = (released_by IS NULL)", name="ck_block_release_pair"
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    task_id: int = Field(foreign_key="task.id", ondelete="CASCADE", index=True)
    reason: str
    created_by: int = Field(foreign_key="user.id")
    created_at: datetime
    released_by: int | None = Field(default=None, foreign_key="user.id")
    released_at: datetime | None = Field(default=None)


# Revision 0001 creates these with the same text. A change here needs a migration.
TRIGGERS = (
    """CREATE TRIGGER agent_owner_is_fixed BEFORE UPDATE OF owner_id ON agent
WHEN NEW.owner_id IS NOT OLD.owner_id
BEGIN SELECT RAISE(ABORT, 'agent.owner_id is fixed'); END""",
    """CREATE TRIGGER agent_owner_is_human BEFORE INSERT ON agent
WHEN NOT EXISTS (SELECT 1 FROM human WHERE user_id = NEW.owner_id)
BEGIN SELECT RAISE(ABORT, 'an agent is owned by a human'); END""",
    """CREATE TRIGGER agent_user_is_not_human BEFORE INSERT ON agent
WHEN EXISTS (SELECT 1 FROM human WHERE user_id = NEW.user_id)
BEGIN SELECT RAISE(ABORT, 'a user has one profile'); END""",
    """CREATE TRIGGER human_user_is_not_agent BEFORE INSERT ON human
WHEN EXISTS (SELECT 1 FROM agent WHERE user_id = NEW.user_id)
BEGIN SELECT RAISE(ABORT, 'a user has one profile'); END""",
    # An agent's name differs, in any case, from its owner's and from the owner's other agents.
    """CREATE TRIGGER agent_name_is_free BEFORE INSERT ON agent
WHEN EXISTS (
  SELECT 1 FROM user AS mine, user AS other
  WHERE mine.id = NEW.user_id
    AND other.name = mine.name COLLATE NOCASE
    AND (other.id = NEW.owner_id
      OR other.id IN (SELECT user_id FROM agent WHERE owner_id = NEW.owner_id))
)
BEGIN SELECT RAISE(ABORT, 'name is taken in this account'); END""",
    # A rename holds the same rule. The account is the owner, or the user itself for a person.
    """CREATE TRIGGER user_name_is_free BEFORE UPDATE OF name ON user
WHEN NEW.name IS NOT OLD.name AND EXISTS (
  SELECT 1 FROM user AS other
  WHERE other.id != NEW.id
    AND other.name = NEW.name COLLATE NOCASE
    AND (other.id = COALESCE((SELECT owner_id FROM agent WHERE user_id = NEW.id), NEW.id)
      OR other.id IN (
        SELECT user_id FROM agent
        WHERE owner_id = COALESCE((SELECT owner_id FROM agent WHERE user_id = NEW.id), NEW.id)
      ))
)
BEGIN SELECT RAISE(ABORT, 'name is taken in this account'); END""",
    """CREATE TRIGGER token_is_fixed BEFORE UPDATE OF user_id, secret_hash, expires_at ON token
WHEN NEW.user_id IS NOT OLD.user_id
  OR NEW.secret_hash IS NOT OLD.secret_hash
  OR NEW.expires_at IS NOT OLD.expires_at
BEGIN SELECT RAISE(ABORT, 'a session row is fixed'); END""",
    """CREATE TRIGGER task_owner_is_human BEFORE INSERT ON task
WHEN NOT EXISTS (SELECT 1 FROM human WHERE user_id = NEW.owned_by)
BEGIN SELECT RAISE(ABORT, 'a task is owned by a human'); END""",
    # created_by moves only when a reassign hands an agent's work to its owner.
    """CREATE TRIGGER task_is_fixed BEFORE UPDATE OF owned_by, created_by, created_at ON task
WHEN NEW.owned_by IS NOT OLD.owned_by
  OR NEW.created_at IS NOT OLD.created_at
  OR (NEW.created_by IS NOT OLD.created_by AND NOT (
    NEW.created_by = NEW.owned_by
    AND EXISTS (SELECT 1 FROM agent WHERE user_id = OLD.created_by AND owner_id = NEW.owned_by)
  ))
BEGIN SELECT RAISE(ABORT, 'a task keeps its owner and creation'); END""",
    """CREATE TRIGGER task_parent_in_account BEFORE INSERT ON task
WHEN NEW.parent_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM task WHERE id = NEW.parent_id AND owned_by = NEW.owned_by)
BEGIN SELECT RAISE(ABORT, 'a parent is in the same account'); END""",
    """CREATE TRIGGER task_parent_in_account_on_move BEFORE UPDATE OF parent_id ON task
WHEN NEW.parent_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM task WHERE id = NEW.parent_id AND owned_by = NEW.owned_by)
BEGIN SELECT RAISE(ABORT, 'a parent is in the same account'); END""",
    """CREATE TRIGGER task_status_is_held_by_a_block BEFORE UPDATE OF status ON task
WHEN NEW.status IS NOT OLD.status
  AND EXISTS (SELECT 1 FROM block WHERE task_id = NEW.id AND released_at IS NULL)
BEGIN SELECT RAISE(ABORT, 'a blocked task keeps its status'); END""",
    """CREATE TRIGGER block_on_an_open_task BEFORE INSERT ON block
WHEN (SELECT status FROM task WHERE id = NEW.task_id) NOT IN (0, 1)
BEGIN SELECT RAISE(ABORT, 'a block is added only to an open task'); END""",
    # released_by moves only when a reassign hands an agent's release to its owner.
    """CREATE TRIGGER block_release_is_final BEFORE UPDATE OF released_at, released_by ON block
WHEN OLD.released_at IS NOT NULL AND (
  NEW.released_at IS NOT OLD.released_at
  OR (NEW.released_by IS NOT OLD.released_by AND NOT (
    NEW.released_by = (SELECT owned_by FROM task WHERE id = NEW.task_id)
    AND EXISTS (
      SELECT 1 FROM agent WHERE user_id = OLD.released_by
        AND owner_id = (SELECT owned_by FROM task WHERE id = NEW.task_id)
    )
  ))
)
BEGIN SELECT RAISE(ABORT, 'a released block stays released'); END""",
    # A block and a history row go only with their task, by the delete cascade.
    """CREATE TRIGGER block_goes_with_its_task BEFORE DELETE ON block
WHEN EXISTS (SELECT 1 FROM task WHERE id = OLD.task_id)
BEGIN SELECT RAISE(ABORT, 'a block is deleted only with its task'); END""",
    """CREATE TRIGGER task_history_goes_with_its_task BEFORE DELETE ON task_history
WHEN EXISTS (SELECT 1 FROM task WHERE id = OLD.task_id)
BEGIN SELECT RAISE(ABORT, 'a history row is deleted only with its task'); END""",
    """CREATE TRIGGER block_is_fixed BEFORE UPDATE OF task_id, created_at ON block
WHEN NEW.task_id IS NOT OLD.task_id OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'a block stays on its task'); END""",
    """CREATE TRIGGER task_history_is_fixed BEFORE UPDATE OF task_id, created_at, state
ON task_history
WHEN NEW.task_id IS NOT OLD.task_id
  OR NEW.created_at IS NOT OLD.created_at
  OR NEW.state IS NOT OLD.state
BEGIN SELECT RAISE(ABORT, 'a history row is fixed'); END""",
)


for _trigger in TRIGGERS:
    event.listen(SQLModel.metadata, "after_create", DDL(_trigger))

# Imported last: the guards need the classes above, and importing them here
# registers the write rules for every session that can touch these tables.
from barrelvault import guards  # noqa: E402, F401
