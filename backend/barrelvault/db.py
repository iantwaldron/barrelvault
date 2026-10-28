"""The process-wide database engine and a session for each unit of work."""

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, create_engine

from barrelvault.settings import load_settings

BUSY_TIMEOUT_MS = 5000
MIGRATIONS = Path(__file__).resolve().parent / "migrations"

_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = build_engine(load_settings().db_path)
    return _engine


def build_engine(db_path: str) -> Engine:
    """An engine for one SQLite file, configured the same way everywhere, tests included."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _configure_connection(dbapi_connection, _record):
        # The driver would skip BEGIN before a SELECT. The begin hook issues it instead.
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        _use_wal(cursor)
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        cursor.close()

    @event.listens_for(engine, "begin")
    def _begin(connection):
        immediate = connection.get_execution_options().get("immediate", False)
        connection.exec_driver_sql("BEGIN IMMEDIATE" if immediate else "BEGIN")

    return engine


def _use_wal(cursor: sqlite3.Cursor) -> None:
    """Switch to WAL. SQLite skips the busy handler here, so wait by hand."""
    deadline = time.monotonic() + BUSY_TIMEOUT_MS / 1000
    while True:
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.fetchall()
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


@contextmanager
def open_session(*, immediate: bool = True) -> Iterator[Session]:
    """Open a database session, commit on success, and roll back on failure.

    A session is immediate unless the caller only reads. It takes the write
    lock from its first statement, so a second writer waits out the busy
    timeout instead of failing, and a check it reads cannot change before its
    write commits. A read-only session passes immediate=False.
    """
    # Objects stay readable after the commit, so a caller can return what it wrote.
    session = Session(get_engine(), expire_on_commit=False)
    if immediate:
        session.connection(execution_options={"immediate": True})
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """Yield the database session for one request."""
    with open_session() as session:
        yield session


def apply_migrations() -> None:
    """Upgrade the database to the newest migration."""
    command.upgrade(migration_config(), "head")


def migration_config() -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    return config
