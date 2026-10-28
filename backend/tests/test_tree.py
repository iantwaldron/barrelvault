from barrelvault.tree import Tree

# 1 ─ 2 ─ 3, and 4 as a second root.
PARENTS = {1: None, 2: 1, 3: 2, 4: None}


def test_ancestors_run_nearest_first_to_the_root():
    tree = Tree(PARENTS)
    assert tree.ancestors(3) == [2, 1]
    assert tree.ancestors(1) == []
    assert tree.ancestors(99) == []
    assert 3 in tree and 99 not in tree
    assert sorted(tree) == [1, 2, 3, 4]


def test_the_cycle_check_refuses_the_task_itself_and_a_descendant():
    tree = Tree(PARENTS)
    assert tree.would_cycle(1, 1)
    assert tree.would_cycle(1, 3)
    assert not tree.would_cycle(3, 4)
    assert not tree.would_cycle(4, 1)


def test_a_stored_loop_ends_the_walk_instead_of_hanging():
    assert Tree({1: 2, 2: 1}).ancestors(1) == [2, 1]
