"""[V2.52] (b): the model sees where the keyboard focus is. A `focused` token on the row of the
element with the focus (a code fact, read every observation, no page text), and a header line
outside the screen fence when no row can carry it: `focus: elsewhere` (on something not listed,
such as the address bar) or `focus: unread`. Observations of each kind are counted."""

from __future__ import annotations

import dataclasses
import json

import pytest

from steerai.loop2.controller import (
    PROMPT_VERSION,
    SYSTEM_RULES,
    build_prompt,
    focus_state,
    v2_listing,
)
from steerai.loop2.decide_port import DecideCalls, Reply, TaskGenerations
from steerai.loop2.loop import TokenUsage, TurnView
from steerai.loop2.prompt_inputs import controller_inputs, make_decide
from steerai.loop2.runner import run_task
from tests.loop2.world_fakes import (
    FIELD,
    GO,
    PAGE,
    QUERY_PAGE,
    World,
    observed,
    snapshot,
    task,
)

SHOWN = snapshot(PAGE)
HANDLES = {e.id: e.id for e in PAGE}
UNLISTED = (9, 999)


def _rows(focus: tuple[int, ...] | None) -> dict[int, str]:
    rows = v2_listing(SHOWN, handles=HANDLES, focus=focus)
    return {int(r.text.split(" ", 1)[0]): r.text for r in rows}


def test_the_focused_row_carries_the_token_and_no_other_row_does() -> None:
    rows = _rows(FIELD.runtime_id)
    assert rows[FIELD.id].endswith(" focused")
    assert [i for i, text in rows.items() if text.endswith(" focused")] == [FIELD.id]


@pytest.mark.parametrize("focus", [None, UNLISTED])
def test_no_row_carries_the_token_when_the_focus_is_unread_or_elsewhere(
    focus: tuple[int, ...] | None,
) -> None:
    assert not any(text.endswith(" focused") for text in _rows(focus).values())


def test_a_listing_without_focus_is_the_listing_it_was() -> None:
    assert v2_listing(SHOWN, handles=HANDLES) == v2_listing(SHOWN, handles=HANDLES, focus=None)


@pytest.mark.parametrize(
    ("focus", "state"),
    [(FIELD.runtime_id, None), (None, "unread"), (UNLISTED, "elsewhere")],
)
def test_the_focus_state_names_only_what_no_row_carries(
    focus: tuple[int, ...] | None, state: str | None
) -> None:
    assert focus_state(SHOWN, focus) == state


def test_an_element_with_no_runtime_id_never_carries_the_focus() -> None:
    blank = dataclasses.replace(GO, runtime_id=())
    shown = snapshot([*PAGE[:2], blank])
    assert focus_state(shown, ()) == "unread"  # one meaning for (): unread, fail closed
    rows = v2_listing(shown, handles={}, focus=())
    assert not any(r.text.endswith(" focused") for r in rows)


@pytest.mark.parametrize("state", ["elsewhere", "unread"])
def test_the_header_line_is_outside_the_screen_fence(state: str) -> None:
    _system, blocks = build_prompt(
        "goal",
        ["s1: x"],
        [],
        [("s1", "open")],
        "Title",
        [],
        task_fence="taskfence01",
        screen_fence="screenfence02",
        focus=state,  # type: ignore[arg-type]  # a str from the parametrize
    )
    turn = blocks[2]
    assert f"focus: {state}" in turn.split("\n")
    assert turn.index(f"focus: {state}") < turn.index("<<<screen ")


def test_no_header_line_when_a_row_carries_the_focus() -> None:
    _system, blocks = build_prompt(
        "goal",
        ["s1: x"],
        [],
        [("s1", "open")],
        "Title",
        [],
        task_fence="taskfence01",
        screen_fence="screenfence02",
    )
    assert "focus:" not in blocks[2]


def test_the_legend_explains_the_token_and_the_header() -> None:
    assert PROMPT_VERSION == "controller-13"
    assert (
        "- `focused` marks the element with the keyboard focus; `focus: elsewhere` means it is "
        "on something not listed (such as the address bar), `focus: unread` that it could not "
        "be read." in SYSTEM_RULES
    )


# ---- through the decide port -----------------------------------------------------------------


def _view(focus: tuple[int, ...] | None) -> TurnView:
    return TurnView(
        observed=observed(focus=focus),
        log=(),
        first_turn=True,
        handles=HANDLES,
        subgoals=(),
        proven=frozenset(),
    )


@pytest.mark.parametrize(
    ("focus", "state"),
    [(FIELD.runtime_id, None), (None, "unread"), (UNLISTED, "elsewhere")],
)
def test_the_controller_inputs_carry_the_focus(
    focus: tuple[int, ...] | None, state: str | None
) -> None:
    inputs = controller_inputs(_view(focus), goal=task().goal)
    assert inputs.focus == state
    marked = [r.text for r in inputs.listing_rows if r.text.endswith(" focused")]
    assert len(marked) == (1 if state is None else 0)


class _Provider:
    def __init__(self) -> None:
        self.blocks: list[list[str]] = []

    def __call__(self, system: list[str], blocks: list[str]) -> Reply:
        del system
        self.blocks.append(blocks)
        usage = TokenUsage(input_tokens=1, output_tokens=1)
        return Reply(text=json.dumps({"stop": "cannot"}), stop_reason="end_turn", usage=usage)


def test_the_prompt_shows_the_focused_row_inside_the_fence() -> None:
    provider = _Provider()
    calls = DecideCalls(
        provider,
        interrupted=lambda: None,
        generations=TaskGenerations(),
        late_error=lambda _t: None,
    )
    make_decide(calls, goal=task().goal, task_fence="taskfence01")(_view(FIELD.runtime_id))
    turn = provider.blocks[0][2]
    start, end = turn.index("<<<screen "), turn.rindex(">>>")
    assert '"Search"' in turn[start:end]
    assert " focused" in turn[start:end]
    assert "focus:" not in turn


# ---- counted for the stage 1 report ----------------------------------------------------------


def test_observations_with_the_focus_unread_or_elsewhere_are_counted() -> None:
    """The first look (focus unread) and the read after the decision (focus elsewhere), then the
    stop: each observation taken in is counted by its focus state."""
    world = World(
        [],
        observations=[observed(QUERY_PAGE, focus=None), observed(QUERY_PAGE, focus=UNLISTED)],
    )
    outcome = run_task(task(), world.ports())
    assert len(world.read_log) == 2
    assert (outcome.observations_focus_unread, outcome.observations_focus_elsewhere) == (1, 1)


def test_an_observation_with_the_focus_on_a_row_is_not_counted() -> None:
    world = World([], observations=[observed(QUERY_PAGE, focus=FIELD.runtime_id)] * 2)
    outcome = run_task(task(), world.ports())
    assert (outcome.observations_focus_unread, outcome.observations_focus_elsewhere) == (0, 0)
