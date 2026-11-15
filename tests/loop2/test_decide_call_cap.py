"""Invariant 9, [V2.69]: budgets count real calls and are never overrun. `decide` makes its
repair call only when the turn still has a call left; without one, the rejection stands as it
is (one call), and the runner's budget check ends the task paused with the budget token."""

from __future__ import annotations

import json

import pytest

from steerai.loop2.controller import ControllerError, ListingRow, decide

GOAL = "expand the Shipping policy"
BAD = json.dumps(
    {
        "subgoals": [
            {"id": "expand_the_shipping_policy", "desc": "x", "closes": "state",
             "params": {"name": "Shipping policy"}}
        ],
        "actions": [{"verb": "expand", "target": 1, "subgoal": "expand_the_shipping_policy"}],
    }
)  # fmt: skip


def _decide(calls_left: int | None) -> tuple[ControllerError, int]:
    made: list[int] = []

    def ask(_system: list[str], _blocks: list[str]) -> str:
        made.append(1)
        return BAD

    with pytest.raises(ControllerError) as rejected:
        decide(ask, task_goal=GOAL, subgoal_lines=[], action_log=[], subgoal_status=[],
               window_title="t", listing_rows=[ListingRow(0, '1 Button "Shipping policy"', 1)],
               listed_ids=frozenset({1}), first_turn=True, task_fence="taskfence01",
               max_calls=calls_left)  # fmt: skip
    return rejected.value, len(made)


def test_with_one_call_left_the_repair_is_not_made() -> None:
    error, made = _decide(1)
    assert made == 1
    assert error.record is not None
    assert (error.record.calls, error.record.repaired) == (1, False)
    assert error.tokens == ("subgoal_id_bad",)


def test_with_two_calls_left_the_repair_is_made() -> None:
    error, made = _decide(2)
    assert made == 2
    assert error.record is not None
    assert error.record.calls == 2


def test_with_no_cap_the_repair_is_made() -> None:
    _error, made = _decide(None)
    assert made == 2
