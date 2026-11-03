"""The runner's parent-pid chain must stop at a reused pid: a parent cannot have started after
its child (RN-15 follow-up; on CI a reused ancestor pid was Chrome's and the runner refused its
own target as its console)."""

from __future__ import annotations

from steerai.probes.windows import parent_chain


def test_the_chain_walks_nearest_first_and_stops_at_the_root() -> None:
    parents = {100: 50, 50: 10, 10: 0}
    started = {100: 500, 50: 400, 10: 300}
    assert parent_chain(100, parents, started.get) == [50, 10]


def test_a_parent_that_started_after_its_child_is_a_reused_pid_and_ends_the_chain() -> None:
    parents = {100: 50, 50: 10, 10: 4}
    started = {100: 500, 50: 400, 10: 900, 4: 1}  # pid 10 was reused by a newer process
    assert parent_chain(100, parents, started.get) == [50]


def test_an_unknown_start_time_ends_the_chain() -> None:
    parents = {100: 50, 50: 10}
    started = {100: 500}
    assert parent_chain(100, parents, started.get) == []


def test_cycles_and_the_limit_are_honoured() -> None:
    parents = {1: 2, 2: 1}
    started = {1: 10, 2: 5}
    assert parent_chain(1, parents, started.get) == [2]
    long = {i: i + 1 for i in range(1, 100)}
    times = {i: 1000 - i for i in range(1, 101)}
    assert len(parent_chain(1, long, times.get, limit=5)) == 5
