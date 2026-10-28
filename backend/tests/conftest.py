"""Fixtures shared by the test modules. Every engine is built the way production builds it."""

import pytest
from sqlmodel import Session, SQLModel

from barrelvault.db import build_engine
from tests.support import reset_engine


@pytest.fixture
def engine(tmp_path):
    engine = build_engine(str(tmp_path / "test.db"))
    SQLModel.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine):
    with Session(engine) as db_session:
        yield db_session


@pytest.fixture
def database(monkeypatch, tmp_path):
    """Point the process engine at a fresh file that no table has been created in."""
    path = tmp_path / "barrelvault.db"
    monkeypatch.setenv("BARRELVAULT_DB", str(path))
    reset_engine()
    yield path
    reset_engine()
