"""User, Human, Agent, and Token tables.

A rule that holds for every write, whoever makes it, is a constraint or a
trigger here, so a bulk statement or raw SQL cannot skip it. A rule that
depends on which call is writing lives in guards.py.
"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import DDL, CheckConstraint, event
from sqlmodel import Field, Session, SQLModel, select


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
)

for _trigger in TRIGGERS:
    event.listen(SQLModel.metadata, "after_create", DDL(_trigger))

# Imported last: the guards need the classes above, and importing them here
# registers the write rules for every session that can touch these tables.
from barrelvault import guards  # noqa: E402, F401
