"""[V2.48]: stage 1 measures, per watch class, how many watches ended at their deadline. The
run's final line carries the counts (closed class tokens and whole numbers only): the redacted
trace holds no per-step lines, and stage 1 reads the final line, never the trace."""

from __future__ import annotations

from typing import get_args

from steerai.loop2.decision import Decision
from steerai.loop2.loop import WatchKind
from steerai.loop2.runner import run_task
from tests.loop2.world_fakes import GO, World, act, decision, task


def test_each_watch_is_counted_by_its_class_with_its_deadline_hits() -> None:
    world = World(
        [decision(act("click", target=GO.id), first=True), decision(act("wait"))],
        watch_timeouts=[True, False],
    )
    outcome = run_task(task(), world.ports())
    counts = {kind: (watches, hits) for kind, watches, hits in outcome.watch_deadlines}
    assert counts["trigger"] == (1, 1)  # a click on a button: its deadline reached
    assert counts["wait"] == (1, 0)
    assert counts["plain"] == (0, 0)
    assert counts["submit"] == (0, 0)


def test_every_class_is_reported_even_when_none_ran() -> None:
    outcome = run_task(task(), World([Decision(stop="cannot")]).ports())
    assert [kind for kind, _w, _h in outcome.watch_deadlines] == list(get_args(WatchKind))
    assert all((w, h) == (0, 0) for _k, w, h in outcome.watch_deadlines)
