"""History: a snapshot of a model's mutable fields each time they change.

@history(TaskHistory) on Task records a row whenever a task is inserted, and whenever
an update changes one of the recorded fields. The row is written on the task's own
connection, inside the flush, so a rollback drops the task write and its snapshot
together. The editor is the row's updated_by, so the recorder reads no session.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TypeVar

from sqlalchemy import JSON, String, event, insert
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Mapper
from sqlalchemy.orm.attributes import instance_state
from sqlmodel import Field, SQLModel

from barrelvault.times import as_utc

# Columns every audited model carries that a snapshot leaves out.
UNRECORDED = frozenset({"id", "created_by", "created_at", "updated_at", "updated_by", "owned_by"})
# A column whose info sets "history" to False is derived from another and left out too.
HISTORY_INFO = "history"

Model = TypeVar("Model", bound=type[SQLModel])


class HistoryBase(SQLModel):
    """The columns every history table has. A history table adds its subject's foreign key."""

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id")
    created_at: datetime
    state: dict[str, Any] = Field(sa_type=JSON)


def history(history_model: type[HistoryBase]) -> Callable[[Model], Model]:
    """Record a snapshot of the decorated model in history_model."""

    def register(model: Model) -> Model:
        table = model.__table__  # type: ignore[attr-defined]
        fields = [
            column.name
            for column in table.columns
            if column.name not in UNRECORDED and column.info.get(HISTORY_INFO, True)
        ]
        # A type decorator such as sqlmodel's AutoString wraps the String it stores.
        text_fields = {
            column.name
            for column in table.columns
            if column.nullable and isinstance(getattr(column.type, "impl", column.type), String)
        }
        subject_key = _subject_key(history_model, table.name)
        target_table = history_model.__table__  # type: ignore[attr-defined]

        def snapshot(row: Any) -> dict[str, Any]:
            state: dict[str, Any] = {}
            for name in fields:
                value = getattr(row, name)
                if isinstance(value, datetime):
                    value = as_utc(value).isoformat()
                elif value is None and name in text_fields:
                    value = ""
                state[name] = value
            return state

        def record(connection: Connection, row: Any) -> None:
            connection.execute(
                insert(target_table).values(
                    {
                        subject_key: row.id,
                        "user_id": row.updated_by,
                        "created_at": datetime.now(UTC),
                        "state": snapshot(row),
                    }
                )
            )

        def after_insert(_mapper: Mapper[Any], connection: Connection, row: Any) -> None:
            record(connection, row)

        def after_update(_mapper: Mapper[Any], connection: Connection, row: Any) -> None:
            state = instance_state(row)
            if any(state.attrs[name].history.has_changes() for name in fields):
                record(connection, row)

        event.listen(model, "after_insert", after_insert)
        event.listen(model, "after_update", after_update)
        return model

    return register


def _subject_key(history_model: type[HistoryBase], subject_table: str) -> str:
    """The one column of the history table that points at the audited table."""
    table = history_model.__table__  # type: ignore[attr-defined]
    keys = [
        column.name
        for column in table.columns
        for foreign_key in column.foreign_keys
        if foreign_key.column.table.name == subject_table
    ]
    assert len(keys) == 1, f"{table.name} needs one foreign key to {subject_table}"
    return keys[0]
