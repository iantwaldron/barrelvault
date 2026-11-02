"""Run the migrations against the application database."""

from alembic import context
from sqlmodel import SQLModel

from barrelvault import models  # noqa: F401  (puts the tables on the metadata)
from barrelvault.db import build_engine
from barrelvault.settings import load_settings

# The alembic command is its own process, so with no engine handed in it loads the settings.
engine = context.config.attributes.get("engine") or build_engine(load_settings().db_path)

# Immediate, so two processes migrating one file take turns. The engine begins
# each transaction itself, so SQLite DDL rolls back with it.
with engine.connect().execution_options(immediate=True) as connection:
    context.configure(
        connection=connection,
        target_metadata=SQLModel.metadata,
        render_as_batch=True,
        transactional_ddl=True,
    )
    with context.begin_transaction():
        context.run_migrations()
