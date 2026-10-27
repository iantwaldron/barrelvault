import sqlite3

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import SQLModel, select

from barrelvault import db
from barrelvault.db import BUSY_TIMEOUT_MS, get_engine, get_session, open_session
from barrelvault.models import Human, User


def _clear_engine():
    engine = db._engine
    if engine is not None:
        engine.dispose()
    db._engine = None


@pytest.fixture(autouse=True)
def database(monkeypatch, tmp_path):
    path = tmp_path / "barrelvault.db"
    monkeypatch.setenv("BARRELVAULT_DB", str(path))
    monkeypatch.delenv("BARRELVAULT_SESSION_LENGTH", raising=False)
    monkeypatch.delenv("BARRELVAULT_KEY_LENGTH", raising=False)
    _clear_engine()
    yield path
    _clear_engine()


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
    _clear_engine()
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
            session.add(Human(user_id=1, email="ada@example.com", password_hash="hash"))


def test_a_read_runs_inside_a_transaction(database):
    with open_session() as session:
        session.execute(text("SELECT 1"))
        assert session.connection().connection.dbapi_connection.in_transaction


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
