from datetime import UTC, datetime, timedelta

from barrelvault.status import COMPLETED, DROPPED, IN_PROGRESS, TODO
from barrelvault.tree import TaskRow, Tree

START = datetime(2026, 1, 1, tzinfo=UTC)


def _row(task_id: int, parent_id: int | None, status: int = TODO, minute: int = 0) -> TaskRow:
    created = START + timedelta(minutes=minute)
    return TaskRow(
        id=task_id,
        parent_id=parent_id,
        title=f"Task {task_id}",
        status=status,
        tags="",
        estimate_minutes=None,
        due_at=None,
        created_by=1,
        created_at=created,
        updated_by=1,
        updated_at=created,
    )


def test_children_and_roots_are_in_list_order():
    tree = Tree([_row(3, None, minute=2), _row(1, None, minute=0), _row(2, None, minute=0)])
    assert [row.id for row in tree.children(None)] == [1, 2, 3]
    assert tree.children(99) == []


def test_descendants_come_parents_first_at_every_depth():
    rows = [_row(1, None), _row(2, 1, minute=1), _row(3, 2), _row(4, 1, minute=2), _row(5, None)]
    tree = Tree(rows)
    assert [row.id for row in tree.descendants(1)] == [2, 3, 4]
    assert [row.id for row in tree.descendants(None)] == [1, 2, 3, 4, 5]
    assert 3 in tree and 99 not in tree
    assert [row.id for row in tree.children(2)] == [3]


def test_a_dropped_ancestor_makes_its_descendants_effectively_dropped():
    rows = [_row(1, None, DROPPED), _row(2, 1, IN_PROGRESS), _row(3, 2, COMPLETED), _row(4, None)]
    tree = Tree(rows)
    assert all(tree.is_effectively_dropped(task_id) for task_id in (1, 2, 3))
    assert not tree.is_effectively_dropped(4)
    assert tree.children(1)[0].status == IN_PROGRESS


def test_reopening_or_moving_out_brings_a_child_back():
    reopened = Tree([_row(1, None, TODO), _row(2, 1, IN_PROGRESS)])
    assert not reopened.is_effectively_dropped(2)
    moved_out = Tree([_row(1, None, DROPPED), _row(2, None, IN_PROGRESS)])
    assert not moved_out.is_effectively_dropped(2)
    moved_back = Tree([_row(1, None, DROPPED), _row(2, 1, IN_PROGRESS)])
    assert moved_back.is_effectively_dropped(2)


def test_completed_does_not_pass_down():
    tree = Tree([_row(1, None, COMPLETED), _row(2, 1, TODO)])
    assert not tree.is_effectively_dropped(2)


def test_the_cycle_check_refuses_the_task_itself_and_a_descendant():
    tree = Tree([_row(1, None), _row(2, 1), _row(3, 2), _row(4, None)])
    assert tree.would_cycle(1, 1)
    assert tree.would_cycle(1, 3)
    assert not tree.would_cycle(3, 4)
    assert not tree.would_cycle(4, 1)
