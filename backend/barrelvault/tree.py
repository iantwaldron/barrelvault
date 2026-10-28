"""The task tree as each task's parent, for the cycle check. It reads no database."""

from collections.abc import Iterator, Mapping


class Tree:
    def __init__(self, parents: Mapping[int, int | None]) -> None:
        """parents maps each task id in the account to its parent's id, or None for a root."""
        self._parents = dict(parents)

    def __contains__(self, task_id: object) -> bool:
        return task_id in self._parents

    def __iter__(self) -> Iterator[int]:
        return iter(self._parents)

    def ancestors(self, task_id: int) -> list[int]:
        """The ids above a task, nearest first, ending at a root."""
        found: list[int] = []
        node = self._parents.get(task_id)
        while node is not None and node in self._parents and node not in found:
            found.append(node)
            node = self._parents[node]
        return found

    def would_cycle(self, task_id: int, parent_id: int) -> bool:
        """True when nesting the task under parent_id would make it its own ancestor."""
        return parent_id == task_id or task_id in self.ancestors(parent_id)
