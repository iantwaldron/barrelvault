"""Helpers shared by the test modules."""

from sqlalchemy import text
from sqlmodel import Session

from barrelvault import auth, db
from barrelvault.account import IssuedKey, add_agent, create_account, issue_key
from barrelvault.auth import authenticate, login
from barrelvault.models import Agent, Human, User
from barrelvault.settings import DEFAULT_KEY_LENGTH, DEFAULT_SESSION_LENGTH

KEY_LENGTH = DEFAULT_KEY_LENGTH
SESSION_LENGTH = DEFAULT_SESSION_LENGTH


def act_as(session: Session, secret: str) -> User:
    """Authenticate the way the API does, then act as that user."""
    return authenticate(session, secret)


def make_account(session: Session) -> tuple[Human, str]:
    human = create_account(session, "Ada", "ada@example.com", "password-1", "America/Chicago")
    secret = login(session, "ada@example.com", "password-1", session_length=SESSION_LENGTH)
    return human, secret


def second_account(session: Session) -> tuple[int, str]:
    """Insert a second human directly, the way the spec tells tests to."""
    connection = session.connection()
    user_id = connection.execute(
        text("INSERT INTO user (name) VALUES ('Bea') RETURNING id")
    ).scalar_one()
    connection.execute(
        text(
            "INSERT INTO human (user_id, email, password_hash, timezone) "
            "VALUES (:user_id, 'bea@example.com', :password_hash, 'UTC')"
        ),
        {"user_id": user_id, "password_hash": auth.hash_password("password-2")},
    )
    return user_id, login(session, "bea@example.com", "password-2", session_length=SESSION_LENGTH)


def live_agent(session: Session, secret: str, name: str = "Bot") -> tuple[Agent, IssuedKey]:
    agent = add_agent(session, act_as(session, secret), name)
    issued = issue_key(session, act_as(session, secret), agent.user_id, key_length=KEY_LENGTH)
    return agent, issued


def update_agent(session: Session, user_id: int, **values: object) -> None:
    update_row(session, "agent", user_id, values)


def set_token_expiry(session: Session, user_id: int, expires_at: str) -> None:
    """A session row is fixed, so swap it for a copy with this expiry."""
    connection = session.connection()
    row = dict(
        connection.execute(
            text("SELECT * FROM token WHERE user_id = :user_id"), {"user_id": user_id}
        )
        .mappings()
        .one()
    )
    connection.execute(text("DELETE FROM token WHERE user_id = :user_id"), {"user_id": user_id})
    row["expires_at"] = expires_at
    connection.execute(
        text(
            "INSERT INTO token (id, user_id, secret_hash, created_at, expires_at) "
            "VALUES (:id, :user_id, :secret_hash, :created_at, :expires_at)"
        ),
        row,
    )
    session.expire_all()


def update_row(session: Session, table: str, user_id: int, values: dict[str, object]) -> None:
    """Write columns with raw SQL, outside the ORM and its guards."""
    assignments = ", ".join(f"{column} = :{column}" for column in values)
    session.connection().execute(
        text(f"UPDATE {table} SET {assignments} WHERE user_id = :user_id"),
        {"user_id": user_id, **values},
    )
    session.expire_all()


def reset_engine() -> None:
    if db._engine is not None:
        db._engine.dispose()
    db._engine = None
