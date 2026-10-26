"""User, Human, Agent, and Token tables."""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from sqlmodel import Field, Session, SQLModel, select

from barrelvault.settings import load_settings


class User(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True)


class Human(SQLModel, table=True):
    user_id: int = Field(foreign_key="user.id", primary_key=True)
    email: str = Field(unique=True)
    password_hash: str

    def get_agents(self, session: Session) -> list["Agent"]:
        """Return the agents this human owns, including retired agents."""
        rows = session.exec(select(Agent).where(Agent.owner_id == self.user_id)).all()
        return list(rows)


class Agent(SQLModel, table=True):
    user_id: int = Field(foreign_key="user.id", primary_key=True)
    owner_id: int = Field(foreign_key="user.id")
    key_hash: str | None = Field(default=None)
    key_expires_at: datetime | None = Field(default=None)
    retired_at: datetime | None = Field(default=None)

    def generate_key(self) -> str:
        """Store a new key hash and expiry. Return the raw key."""
        raw = secrets.token_hex(32)
        self.key_hash = hashlib.sha256(raw.encode()).hexdigest()
        length = load_settings().key_length
        self.key_expires_at = datetime.now(UTC) + timedelta(seconds=length)
        return raw


class Token(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", unique=True)
    secret_hash: str = Field(unique=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime

    def generate_secret(self) -> str:
        """Store a new secret hash and expiry. Return the raw secret."""
        raw = secrets.token_hex(32)
        minted = datetime.now(UTC)
        self.secret_hash = hashlib.sha256(raw.encode()).hexdigest()
        self.created_at = minted
        self.expires_at = minted + timedelta(seconds=load_settings().session_length)
        return raw
