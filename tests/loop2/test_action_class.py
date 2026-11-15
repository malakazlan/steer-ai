"""[V2.50]: the risk path classes an action once (`action_class`), and that one call gives both
its risk handling and its watch timing. A press that activates an acting element (Enter on a
Button, Hyperlink, MenuItem or SplitButton; Space on a non-field that is not state-set) is a
trigger, as a click on a non-field is. A Space or an Enter on a state-set element (CheckBox,
RadioButton, ListItem, TabItem, TreeItem) performs a state verb with a readable state: not a
trigger. `watch_kind` only reads the result the risk path put on the dispatch.
"""

from __future__ import annotations

import itertools

import pytest

from steerai.loop2 import loop
from steerai.loop2.form import ACTING_ROLES, STATE_SET_VERB
from steerai.loop2.loop import TRIGGER_CLASSES, Dispatch, action_class, action_risk, watch_kind
from steerai.loop2.runner import run_task
from steerai.verify.predicates import FIELD_ROLES
from tests.loop2.world_fakes import (
    GO,
    PAGE,
    World,
    act,
    decision,
    element,
    observed,
    report,
    task,
)

ROLES = sorted(ACTING_ROLES | set(STATE_SET_VERB) | FIELD_ROLES | {"Group", "Text", "Image"})
VERBS = ["click", "focus", "check", "uncheck", "select", "expand", "collapse", "press"]
KEYS = ["Enter", "Space", "Tab", "Down", "Escape"]
GOAL = "search the shop for red shoes"


def _combos() -> list[tuple[str, str | None, str]]:
    out: list[tuple[str, str | None, str]] = []
    for verb, role in itertools.product(VERBS, ROLES):
        keys: list[str | None] = list(KEYS) if verb == "press" else [None]
        out += [(verb, key, role) for key in keys]
    return out


def _target(role: str) -> object:
    return element(70, role, "Thing", (9, 70), 1)


def _dispatch(verb: str, key: str | None, role: str, *, trigger: bool) -> Dispatch:
    target = element(70, role, "Thing", (9, 70), 1)
    action = act(verb, key=key) if verb == "press" else act(verb, target=target.id)
    return Dispatch(
        action=action,
        runtime_id=target.runtime_id,
        role=role,
        name=target.name,
        risk="R0",
        provenance=None,
        trigger=trigger,
    )


@pytest.mark.parametrize(("verb", "key", "role"), _combos())
def test_the_timing_and_the_risk_class_come_from_one_call_and_agree(
    verb: str, key: str | None, role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str | None, str]] = []
    real = loop.action_class

    def spy(action: object, role_: str) -> str:
        calls.append((verb, key, role_))
        return real(action, role_)  # type: ignore[arg-type]  # forwarding the spied call

    monkeypatch.setattr(loop, "action_class", spy)
    target = element(70, role, "Thing", (9, 70), 1)
    action = act(verb, key=key) if verb == "press" else act(verb, target=target.id)
    verdict = action_risk(action, target, goal=GOAL, observed=observed([*PAGE, target]), pending=())
    assert calls == [(verb, key, role)]  # the risk path classed it exactly once
    assert verdict.trigger == (real(action, role) in TRIGGER_CLASSES)
    calls.clear()
    timing = watch_kind(_dispatch(verb, key, role, trigger=verdict.trigger))
    assert calls == []  # the timing only reads what the risk path decided
    assert (timing == "trigger") == verdict.trigger


@pytest.mark.parametrize("role", sorted(ACTING_ROLES))
@pytest.mark.parametrize("key", ["Enter", "Space"])
def test_a_press_that_activates_an_acting_element_is_a_trigger(role: str, key: str) -> None:
    assert action_class(act("press", key=key), role) in TRIGGER_CLASSES


@pytest.mark.parametrize("role", sorted(STATE_SET_VERB))
@pytest.mark.parametrize("key", ["Enter", "Space"])
def test_a_press_on_a_state_set_element_is_not_a_trigger(role: str, key: str) -> None:
    """[V2.50] corollary: it performs a state verb with a readable state."""
    assert action_class(act("press", key=key), role) not in TRIGGER_CLASSES


@pytest.mark.parametrize("role", sorted(FIELD_ROLES))
def test_a_space_or_a_click_on_a_field_is_not_a_trigger(role: str) -> None:
    assert action_class(act("press", key="Space"), role) not in TRIGGER_CLASSES
    assert action_class(act("click", target=1), role) not in TRIGGER_CLASSES


def test_a_space_on_a_button_reaches_the_watch_as_a_trigger() -> None:
    """End to end: the risk path's classification travels on the dispatch to the watch."""
    world = World(
        [decision(act("click", target=GO.id), act("press", key="Space"), first=True)],
        effects=[report("focus"), report("none")],
        screen=observed(PAGE, focus=GO.runtime_id),
    )
    run_task(task(), world.ports())
    assert [(d.action.verb, d.trigger, watch_kind(d)) for d in world.dispatched] == [
        ("click", True, "trigger"),
        ("press", True, "trigger"),
    ]


def test_an_enter_on_a_checkbox_reaches_the_watch_as_plain() -> None:
    box = element(42, "CheckBox", "Shop", (9, 42), 1)
    world = World(
        [decision(act("click", target=box.id), act("press", key="Enter"), first=True)],
        effects=[report("own_state"), report("own_state")],
        screen=observed([*PAGE, box], focus=box.runtime_id),
    )
    run_task(task(), world.ports())
    presses = [d for d in world.dispatched if d.action.verb == "press"]
    assert [(d.trigger, watch_kind(d)) for d in presses] == [(False, "plain")]


@pytest.mark.parametrize("role", ["Group", "Text", "Image", "Pane"])
def test_an_enter_on_any_other_role_is_r2_and_held_as_a_trigger(role: str) -> None:
    """[V2.26] (3): R2 (so never sent in step 3); were it sent, an Enter activates: a trigger."""
    action = act("press", key="Enter")
    assert action_class(action, role) == "enter_other"
    assert "enter_other" in TRIGGER_CLASSES
