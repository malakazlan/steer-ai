"""[V2.15] Q1: v1's action keys stay byte-identical when the ledger is generalised for v2. Fixed
values, computed on main before step 4; these pass now and must keep passing."""

from __future__ import annotations

from steerai.orchestrator.contracts import Step, Target
from steerai.orchestrator.ledger import action_key, key_params

SECRET = bytes(range(32))


def test_a_click_key_is_unchanged() -> None:
    key = action_key(SECRET, "click", Target(role="Button", name="Send"), {})
    assert key == "1c953a3f129179724abe4937713c49fc675d7cb9e6e0cbfa435d2c908f9ae8f9"


def test_a_type_key_with_an_automation_id_and_text_is_unchanged() -> None:
    target = Target(role="Edit", name="Message", automation_id="msg")
    key = action_key(SECRET, "type", target, {"text": "ok thanks"})
    assert key == "9823c6b4f8e305ac6848f776e992326b3b8ae6846179504fb3cd66ec1858e260"


def test_a_press_key_with_a_window_ref_is_unchanged() -> None:
    target = Target(role="Edit", name="Search")
    key = action_key(SECRET, "press", target, {"key": "Enter", "_window": "opened:s1"})
    assert key == "327c53d14e3f70b6f86cd79bbe4e5882038966af9b5899d8a0bd019d10c6d318"


def test_a_step_key_through_key_params_is_unchanged() -> None:
    target = Target(role="Edit", name="Message")
    step = Step(id="s1", subgoal="g1", verb="type", target=target, text="ok thanks")
    params = key_params(step, "opened:s0")
    assert params == {"text": "ok thanks", "_window": "opened:s0"}
    key = action_key(SECRET, step.verb, target, params)
    assert key == "d579f096de3e9ae454ebe0132c66286ebf8a72076dcc7eb369f867c59d97ecde"
