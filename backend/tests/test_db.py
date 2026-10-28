import sqlite3
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import SQLModel, select

from barrelvault import db
from barrelvault.db import (
    BUSY_TIMEOUT_MS,
    apply_migrations,
    get_engine,
    get_session,
    migration_config,
    open_session,
)
from barrelvault.models import Token, User
from tests.support import reset_engine


def test_one_engine_for_the_process(database):
    assert get_engine() is get_engine()
    assert not database.exists()


def test_database_file_is_created_when_a_session_connects(database):
    with open_session() as session:
        session.connection()
    assert database.is_file()


def test_missing_parent_directory_is_created(monkeypatch, tmp_path):
    path = tmp_path / "nested" / "barrelvault.db"
    monkeypatch.setenv("BARRELVAULT_DB", str(path))
    reset_engine()
    with open_session() as session:
        session.connection()
    assert path.is_file()


def test_connection_pragmas(database):
    with open_session() as session:
        connection = session.connection()
        foreign_keys = connection.execute(text("PRAGMA foreign_keys")).scalar_one()
        journal_mode = connection.execute(text("PRAGMA journal_mode")).scalar_one()
        busy_timeout = connection.execute(text("PRAGMA busy_timeout")).scalar_one()
    assert foreign_keys == 1
    assert journal_mode == "wal"
    assert busy_timeout == BUSY_TIMEOUT_MS


def test_open_session_commits(database):
    SQLModel.metadata.create_all(get_engine())
    with open_session() as session:
        session.add(User(name="Ada"))
    with open_session() as session:
        assert session.exec(select(User)).one().name == "Ada"


def test_objects_stay_readable_after_the_session_commits(database):
    SQLModel.metadata.create_all(get_engine())
    with open_session() as session:
        user = User(name="Ada")
        session.add(user)
    assert user.id is not None
    assert user.name == "Ada"


def test_open_session_rolls_back(database):
    SQLModel.metadata.create_all(get_engine())
    with pytest.raises(RuntimeError):
        with open_session() as session:
            session.add(User(name="Ada"))
            raise RuntimeError("fail")
    with open_session() as session:
        assert session.exec(select(User)).all() == []


def test_get_session_commits(database):
    SQLModel.metadata.create_all(get_engine())
    generator = get_session()
    session = next(generator)
    session.add(User(name="Ada"))
    with pytest.raises(StopIteration):
        next(generator)
    with open_session() as session:
        assert session.exec(select(User)).one().name == "Ada"


def test_foreign_keys_are_enforced(database):
    SQLModel.metadata.create_all(get_engine())
    with pytest.raises(IntegrityError):
        with open_session() as session:
            session.add(Token(user_id=1, secret_hash="hash", expires_at=datetime.now(UTC)))


def test_a_read_runs_inside_a_transaction(database):
    with open_session() as session:
        session.execute(text("SELECT 1"))
        assert session.connection().connection.dbapi_connection.in_transaction


def test_sessions_that_read_then_write_take_turns(database):
    SQLModel.metadata.create_all(get_engine())
    first_read = threading.Event()
    second_read = threading.Event()
    errors: list[BaseException] = []

    def first() -> None:
        with open_session() as session:
            session.exec(select(User)).all()
            first_read.set()
            # A deferred second session reads now. An immediate one is still
            # waiting for this lock, so the wait times out and this one writes.
            second_read.wait(timeout=0.5)
            session.add(User(name="Ada"))

    def second() -> None:
        first_read.wait(timeout=5)
        try:
            with open_session() as session:
                session.exec(select(User)).all()
                second_read.set()
                session.add(User(name="Bea"))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert errors == []
    with open_session(immediate=False) as session:
        assert {user.name for user in session.exec(select(User))} == {"Ada", "Bea"}


def test_an_immediate_session_holds_the_write_lock(database):
    SQLModel.metadata.create_all(get_engine())
    with open_session(immediate=True) as session:
        session.execute(text("SELECT 1"))
        other = sqlite3.connect(database, timeout=0)
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            other.execute("BEGIN IMMEDIATE")
        other.close()
    other = sqlite3.connect(database, timeout=0)
    other.execute("BEGIN IMMEDIATE")
    other.rollback()
    other.close()


def test_migrations_build_the_model_schema(database):
    apply_migrations()
    apply_migrations()
    with get_engine().connect() as connection:
        differences = compare_metadata(MigrationContext.configure(connection), SQLModel.metadata)
        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert differences == []
    assert version == "0001"


def test_migrations_build_the_model_triggers_and_checks(database, engine):
    """compare_metadata skips triggers and CHECK constraints, so compare them here."""
    apply_migrations()

    def schema(connection) -> dict[str, set]:
        rows = connection.execute(
            text("SELECT type, name, sql FROM sqlite_master WHERE type IN ('trigger', 'table')")
        ).all()
        return {
            "triggers": {(name, sql) for kind, name, sql in rows if kind == "trigger"},
            "checks": {
                name
                for kind, _table, sql in rows
                if kind == "table"
                for name in ("ck_agent_key_pair", "ck_agent_retired_without_key")
                if name in sql
            },
        }

    with get_engine().connect() as migrated, engine.connect() as modeled:
        assert schema(migrated) == schema(modeled)
        assert len(schema(migrated)["triggers"]) == 5
        assert len(schema(migrated)["checks"]) == 2


def test_migrations_downgrade_to_an_empty_database(database):
    apply_migrations()
    command.downgrade(migration_config(), "base")
    with get_engine().connect() as connection:
        names = connection.execute(
            text("SELECT name FROM sqlite_master WHERE type = 'table'")
        ).scalars()
        assert set(names) == {"alembic_version"}


def test_two_processes_can_migrate_one_file(database):
    script = "from barrelvault.db import apply_migrations; apply_migrations()"
    runs = [
        subprocess.Popen(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    for run in runs:
        _out, err = run.communicate(timeout=30)
        assert run.returncode == 0, err
    with get_engine().connect() as connection:
        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert version == "0001"


class _BusyCursor:
    def __init__(self, failures: list[str]) -> None:
        self.failures = failures
        self.calls = 0

    def execute(self, _statement: str) -> None:
        self.calls += 1
        if self.failures:
            raise sqlite3.OperationalError(self.failures.pop(0))

    def fetchall(self) -> list[object]:
        return []


def test_the_wal_switch_waits_out_a_locked_file():
    cursor = _BusyCursor(["database is locked", "database is locked"])
    db._use_wal(cursor)
    assert cursor.calls == 3


def test_the_wal_switch_gives_up_at_the_busy_timeout(monkeypatch):
    monkeypatch.setattr(db, "BUSY_TIMEOUT_MS", 0)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        db._use_wal(_BusyCursor(["database is locked"]))


def test_the_wal_switch_raises_other_errors():
    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        db._use_wal(_BusyCursor(["disk I/O error"]))
