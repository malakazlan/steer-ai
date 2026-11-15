"""[V2.64] Two: the pre-task draft count ([V2.37]'s recorded limit), with no sweep at start. At a
non-field click, the filled state of the fields in that click's submit scope ([V2.22]: under the
field's parent container, or sharing its nearest landmark; unknown reads as inside) is read, and
the click is counted when its scope holds a filled field Steer never typed into. Secure fields
are never read."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from steerai.loop2 import cli
from steerai.loop2.decision import Decision, SubgoalDoc
from steerai.loop2.loop import TaskOutcome
from steerai.loop2.runner import run_task
from steerai.loop2.stage1 import accept
from tests.loop2.test_cli_v2 import Run
from tests.loop2.test_stage1 import DONE, _final, _ok
from tests.loop2.world_fakes import (
    BAR,
    FIELD,
    GO,
    PAGE,
    PASSWORD,
    TYPED_VALUE,
    World,
    act,
    element,
    observed,
    report,
    task,
)

SUBS = [SubgoalDoc(id="s1", desc="x", params={"text": "red shoes"}, closes="page")]


def _clicks_go(*, prefilled: bool, typed_first: bool = False) -> World:
    actions = [act("type", target=FIELD.id, param="text")] if typed_first else []
    world = World(
        [
            Decision(subgoals=SUBS, actions=actions or [act("click", target=GO.id)]),
            *([Decision(actions=[act("click", target=GO.id)])] if typed_first else []),
        ],
        effects=[report("own_value"), report("content_changed")],
    )
    if prefilled:
        world.values[FIELD.runtime_id] = TYPED_VALUE
    return world


def test_a_click_whose_scope_holds_a_field_filled_before_the_task_is_counted() -> None:
    world = _clicks_go(prefilled=True)
    outcome = run_task(task(), world.ports())
    assert outcome.pre_task_draft_clicks == 1
    assert FIELD.runtime_id in world.reads


def test_text_steer_typed_is_not_a_pre_task_draft() -> None:
    world = _clicks_go(prefilled=True, typed_first=True)
    outcome = run_task(task(), world.ports())
    assert outcome.pre_task_draft_clicks == 0


def test_an_empty_field_in_scope_counts_nothing() -> None:
    outcome = run_task(task(), _clicks_go(prefilled=False).ports())
    assert outcome.pre_task_draft_clicks == 0


def test_a_secure_field_is_never_read() -> None:
    world = _clicks_go(prefilled=True)
    run_task(task(), world.ports())
    assert PASSWORD.runtime_id not in world.reads


def test_the_address_bar_is_outside_every_clicks_scope() -> None:
    """The browser frame's address bar holds the URL; it is outside the page's top Document."""
    world = _clicks_go(prefilled=False)
    world.values[BAR.runtime_id] = TYPED_VALUE
    outcome = run_task(task(), world.ports())
    assert BAR.runtime_id not in world.reads
    assert outcome.pre_task_draft_clicks == 0


def test_a_field_outside_the_clicks_scope_is_not_read() -> None:
    """Another container and no shared landmark: outside the [V2.22] scope."""
    group = element(31, "Group", "Notes", (9, 31), 1)
    notes = element(30, "Edit", "Notes", (9, 30), 31)
    page = [p for p in PAGE if p.id != FIELD.id] + [group, notes]
    screen = dataclasses.replace(observed(page), landmarks={e.runtime_id: None for e in page})
    world = World([Decision(subgoals=SUBS, actions=[act("click", target=GO.id)])],
                  effects=[report("content_changed")], screen=screen)  # fmt: skip
    world.values[notes.runtime_id] = TYPED_VALUE
    outcome = run_task(task(), world.ports())
    assert notes.runtime_id not in world.reads
    assert outcome.pre_task_draft_clicks == 0


def test_a_click_on_a_field_is_not_a_non_field_click() -> None:
    world = World(
        [Decision(subgoals=SUBS, actions=[act("click", target=FIELD.id)])],
        effects=[report("focus")],
    )
    world.values[FIELD.runtime_id] = TYPED_VALUE
    outcome = run_task(task(), world.ports())
    assert outcome.pre_task_draft_clicks == 0


def test_the_final_line_and_the_summary_carry_the_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    outcome = TaskOutcome(status="done", cause=None, dispatched=(), model_calls=1, strikes=(),
                          pre_task_draft_clicks=2)  # fmt: skip
    monkeypatch.setattr(cli, "run_task", lambda _task, _ports: outcome)
    run = Run(tmp_path)
    run()
    line = run.result()
    assert line["counts"]["pre_task_draft_clicks"] == 2
    runs = [_ok(DONE, i + 1, final=_final(counts=line["counts"])) for i in range(3)]
    report_ = accept([DONE], runs, controls=(5, 5), runs_per_task=3, label="t").report
    json.dumps(report_, allow_nan=False)
    assert report_["counts"]["pre_task_draft_clicks"] == 6
