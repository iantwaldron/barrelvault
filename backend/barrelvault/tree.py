"""The task tree, built from one account's rows with no database access.

A read loads the account's tasks once and builds a Tree from the rows. The same Tree answers the
one-level read, the subtree read, the cycle check, and which tasks are effectively
dropped. A later version may load a subtree with a recursive query and build the same Tree.
"""

from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime

from barrelvault.status import DROPPED


@dataclass(frozen=True)
class TaskRow:
    """The columns the tree needs. notes is left out, since it can be long."""

    id: int
    parent_id: int | None
    title: str
    status: int
    tags: str
    estimate_minutes: int | None
    due_at: datetime | None
    created_by: int
    created_at: datetime
    updated_by: int
    updated_at: datetime


class Tree:
    def __init__(self, rows: Iterable[TaskRow]) -> None:
        self._rows: dict[int, TaskRow] = {}
        self._children: dict[int | None, list[TaskRow]] = defaultdict(list)
        for row in rows:
            self._rows[row.id] = row
            self._children[row.parent_id].append(row)
        for siblings in self._children.values():
            siblings.sort(key=lambda row: (row.created_at, row.id))
        self._dropped = self._find_dropped()

    def __contains__(self, task_id: object) -> bool:
        return task_id in self._rows

    def children(self, parent_id: int | None) -> list[TaskRow]:
        """The direct children of a task, or the roots for None, in list order."""
        return list(self._children.get(parent_id, ()))

    def descendants(self, parent_id: int | None) -> list[TaskRow]:
        """Every task under a task, or every task for None, parents before children."""
        return list(self._walk(parent_id))

    def is_effectively_dropped(self, task_id: int) -> bool:
        """True when the task or any ancestor is dropped."""
        return task_id in self._dropped

    def would_cycle(self, task_id: int, parent_id: int) -> bool:
        """True when nesting the task under parent_id would make it its own ancestor."""
        node: int | None = parent_id
        while node is not None:
            if node == task_id:
                return True
            node = self._rows[node].parent_id if node in self._rows else None
        return False

    def _walk(self, parent_id: int | None) -> Iterator[TaskRow]:
        stack = list(reversed(self._children.get(parent_id, ())))
        while stack:
            row = stack.pop()
            yield row
            stack.extend(reversed(self._children.get(row.id, ())))

    def _find_dropped(self) -> set[int]:
        dropped: set[int] = set()
        stack = [(row, False) for row in self._children.get(None, ())]
        while stack:
            row, under_dropped = stack.pop()
            here = under_dropped or row.status == DROPPED
            if here:
                dropped.add(row.id)
            stack.extend((child, here) for child in self._children.get(row.id, ()))
        return dropped
