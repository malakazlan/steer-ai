"""[V2.43] (1), #298, slice 6 design point 7: after a Space or an Enter on a state-set element
(CheckBox, RadioButton, ListItem, TabItem, TreeItem), focus should still be on the target. When the
watch's after-read shows it elsewhere, the model sees `focus_moved_after_press:<handle>` (a typed
history record) and the outcome counts it. Arrow keys are exempt (they move focus by design); an
unread focus is not counted; there is no extra read.

Also #339's review: a `scroll` decision through `run_task` with the live act port sends nothing.
"""

from __future__ import annotations

import pytest

from steerai.loop2.act_port import ActPort
from steerai.loop2.history import FocusMovedAfterPress, Vocab, record_ok, render
from steerai.loop2.runner import run_task
from tests.loop2.test_act_port import Desk
from tests.loop2.test_act_port import Executor as RecordingExecutor
from tests.loop2.test_act_port import World as PortWorld
from tests.loop2.world_fakes import (
    PAGE,
    World,
    act,
    decision,
    element,
    observed,
    report,
    task,
)

BOX = element(42, "CheckBox", "Shop", (9, 42), 1)
RADIO = element(43, "RadioButton", "Pro", (9, 43), 1)
LINK = element(44, "Hyperlink", "More", (9, 44), 1)


def _run(target: object, key: str, *, focus_after: object) -> tuple[World, object]:
    """Click the target (focus lands on it), then press `key`; the press's watch shows the
    focus at `focus_after` (a RuntimeId, or None: unread)."""
    page = [*PAGE, BOX, RADIO, LINK]
    world = World(
        [decision(act("click", target=target.id), first=True), decision(act("press", key=key))],  # type: ignore[attr-defined]  # an Element
        effects=[report("own_state"), report("own_state")],
        screen=observed(page, focus=target.runtime_id),  # type: ignore[attr-defined]  # an Element
        later=observed(page, focus=focus_after),  # type: ignore[arg-type]  # a RuntimeId or None
        later_after_acts=2,
    )
    outcome = run_task(task(), world.ports())
    return world, outcome


@pytest.mark.parametrize("target", [BOX, RADIO])
@pytest.mark.parametrize("key", ["Space", "Enter"])
def test_focus_leaving_a_state_set_element_after_its_press_is_shown_and_counted(
    target: object, key: str
) -> None:
    world, outcome = _run(target, key, focus_after=LINK.runtime_id)
    handle = world.views[-1].handles[target.id]  # type: ignore[attr-defined]  # an Element
    assert f"focus_moved_after_press:{handle}" in world.views[-1].history
    assert outcome.focus_moved_after_press == 1  # type: ignore[attr-defined]  # a TaskOutcome


@pytest.mark.parametrize(
    ("target", "key", "focus_after"),
    [
        (BOX, "Space", BOX.runtime_id),  # focus stayed on the target
        (RADIO, "Down", LINK.runtime_id),  # an arrow moves focus by design: exempt
        (LINK, "Enter", BOX.runtime_id),  # not a state-set element
        (BOX, "Space", None),  # the focus was not read: never counted as moved
    ],
)
def test_no_count_when_the_rule_does_not_apply(
    target: object, key: str, focus_after: object
) -> None:
    world, outcome = _run(target, key, focus_after=focus_after)
    assert not any(line.startswith("focus_moved_after_press") for line in world.views[-1].history)
    assert outcome.focus_moved_after_press == 0  # type: ignore[attr-defined]  # a TaskOutcome


def test_the_record_is_closed_like_every_history_record() -> None:
    vocab = Vocab({"s1": frozenset()}, frozenset({42}))
    assert render(FocusMovedAfterPress(42)) == "focus_moved_after_press:42"
    assert record_ok(FocusMovedAfterPress(42), vocab)
    assert not record_ok(FocusMovedAfterPress(99), vocab)  # not a handle this task issued


# ---- #339 review: a scroll through run_task with the live act port sends nothing ----------------


def test_a_scroll_decision_through_run_task_sends_nothing() -> None:
    world = World([decision(act("scroll", target=PAGE[1].id), first=True)])
    semantic, physical = RecordingExecutor(), RecordingExecutor()
    port = ActPort(
        world=PortWorld(world.screen),
        semantic=semantic,
        physical=physical,
        desk=Desk(),
        interrupted=lambda: None,
        clock=lambda: 0.0,
        wall_left=lambda: 60.0,
    )
    ports = world.ports()
    import dataclasses  # noqa: PLC0415 - local to the one test that swaps a port

    run_task(task(), dataclasses.replace(ports, act=port.act))
    assert semantic.calls == physical.calls == []
    assert any(line.startswith("refused:not_in_step") for line in world.views[-1].history)
