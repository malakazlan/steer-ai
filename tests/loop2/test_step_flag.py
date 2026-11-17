"""[V2.86] (4), [V2.88]: the step flag (PR 4a.3). Step 3 is the default everywhere; step 4 is
chosen by `Task.step`, refused at the command line while `ready(4)` is False, and builds its
wording beside step 3's by listed substitutions only. Nothing on the step 3 path changes: the
pins in `test_step3_pins.py` hold that."""

from __future__ import annotations

import dataclasses
import functools
import inspect
import json
from pathlib import Path
from typing import Any

import pytest

from steerai.goal_match import Verdict
from steerai.loop2 import controller, ready
from steerai.loop2.claims import Phase
from steerai.loop2.cli import parse_v2
from steerai.loop2.consent import GoalRow
from steerai.loop2.decision import (
    CHECKED_KINDS,
    NOT_IN_STEP_VERBS,
    NOT_IN_STEP_VERBS_BY_STEP,
    STEP,
    STEP_KINDS,
    STEP_VERBS,
    Claim,
    Decision,
    validate,
)
from steerai.loop2.loop import Reason, Step4Ports, StepPortsMismatchError, TaskOutcome
from steerai.loop2.record import V2Record
from steerai.loop2.runner import run_task
from steerai.loop2.task import Step, Task
from steerai.orchestrator.contracts import GateDecision
from steerai.orchestrator.gate import Ask
from steerai.orchestrator.ledger import Admission
from steerai.run import EXIT_USAGE, UsageError
from tests.loop2.test_cli_v2 import WHERE, Run
from tests.loop2.world_fakes import GO, World, act, decision, task

GOAL = "type Ada Lovelace as the full name"


@pytest.fixture(autouse=True)
def _localappdata(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """As `test_cli_v2.py`: `parse_v2` without `--price-file` reads LOCALAPPDATA, which a Linux
    CI runner does not set; every test here runs with its own."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))


# ---- the flag's default ----------------------------------------------------------------------


def test_a_task_is_step_3_unless_it_says_otherwise() -> None:
    assert task().step == 3
    assert dataclasses.replace(task(), step=4).step == 4
    assert [f.name for f in dataclasses.fields(Task)][-1] == "step"


def test_the_command_line_step_defaults_to_3_and_takes_only_3_or_4() -> None:
    assert parse_v2(WHERE).step == 3
    assert parse_v2([*WHERE, "--step", "4"]).step == 4
    for bad in ("5", "2", "x"):
        with pytest.raises(UsageError):
            parse_v2([*WHERE, "--step", bad])


def test_validate_and_build_prompt_default_to_step_3() -> None:
    for function in (validate, controller.build_prompt, controller.build_audit_prompt):
        assert inspect.signature(function).parameters["step"].default == 3
    for first in (True, False):
        assert controller._prompt_schema(first_turn=first) == controller._prompt_schema(
            first_turn=first, step=3
        )
    rows = [controller.ListingRow(0, '1 edit "Full name"', handle=1)]
    plain = controller.build_prompt(GOAL, [], [], [], "Profile", rows,
                                    task_fence="f" * 16, screen_fence="s" * 16)  # fmt: skip
    keyed = controller.build_prompt(GOAL, [], [], [], "Profile", rows,
                                    task_fence="f" * 16, screen_fence="s" * 16, step=3)  # fmt: skip
    assert plain == keyed


# ---- the command line refuses step 4 while it is not ready ------------------------------------


def test_step_4_refuses_at_the_command_line_while_ready_4_is_false(tmp_path: Path) -> None:
    assert ready(4) is False
    run = Run(tmp_path)
    run.deps = dataclasses.replace(run.deps, ready=ready)
    assert run("--step", "4") == EXIT_USAGE
    assert (run.result()["code"], run.result()["detail"]) == (None, "loop_v2_not_ready")
    assert "step 4" in run.stderr.getvalue()
    assert run.platform.calls == []  # nothing was built


def _canned(seen: list[Task]) -> Any:  # Any: a stand-in for `run_task`
    def fake(task_: Task, _ports: object) -> TaskOutcome:
        seen.append(task_)
        return TaskOutcome(
            status="cannot", cause=Reason("controller_cannot"), dispatched=(), model_calls=1,
            strikes=(), tokens=10.0, cost_usd=0.0, wall_s=1.0,
        )  # fmt: skip

    return fake


def test_the_run_header_records_step_3_and_its_prompt_versions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[Task] = []
    monkeypatch.setattr("steerai.loop2.cli.run_task", _canned(seen))
    run = Run(tmp_path)
    run()
    header = run.header()
    assert (header["step"], header["prompt_version"], header["audit_prompt_version"]) == (
        "3",
        "controller-15",
        "audit-6",
    )
    assert [t.step for t in seen] == [3]


def test_with_step_4_ready_the_task_and_header_carry_step_4(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The flag's plumbing only: a test `ready` says step 4 is built."""
    seen: list[Task] = []
    monkeypatch.setattr("steerai.loop2.cli.run_task", _canned(seen))
    run = Run(tmp_path)
    run("--step", "4")
    header = run.header()
    assert (header["step"], header["prompt_version"], header["audit_prompt_version"]) == (
        "4",
        "controller-s4-1",
        "audit-s4-1",
    )
    assert [t.step for t in seen] == [4]


# ---- the keyed tables -------------------------------------------------------------------------


def test_step_4_offers_content_present_and_step_3_does_not() -> None:
    assert STEP == 3
    assert "content_present" in STEP_KINDS[4]
    assert "content_present" not in STEP_KINDS[3]
    assert STEP_KINDS[4] == STEP_KINDS[3] | {"content_present"}
    assert STEP_VERBS[4] == STEP_VERBS[3]
    assert NOT_IN_STEP_VERBS_BY_STEP[4] == NOT_IN_STEP_VERBS_BY_STEP[3] == NOT_IN_STEP_VERBS
    assert STEP_KINDS[3] == CHECKED_KINDS


def test_validate_refuses_content_present_in_step_3_and_takes_it_in_step_4() -> None:
    claim = Claim(subgoal="s1", kind="content_present", evidence=[1], param="text")
    turn = Decision(claims=[claim])
    known = {"s1": frozenset({"text"})}
    kwargs: dict[str, Any] = {  # Any: validate's keyword arguments
        "listed_ids": frozenset({1}), "goal": GOAL, "first_turn": False, "known_subgoals": known,
    }  # fmt: skip
    assert validate(turn, **kwargs) == ["not_in_step"]
    assert validate(turn, **kwargs, step=3) == ["not_in_step"]
    assert validate(turn, **kwargs, step=4) == []


def test_the_step_4_schema_is_the_step_3_schema_plus_the_content_present_claim() -> None:
    for first in (True, False):
        three = controller._prompt_schema(first_turn=first, step=3)
        four = controller._prompt_schema(first_turn=first, step=4)
        assert "Claim_content_present" not in three["$defs"]
        added = four["$defs"].pop("Claim_content_present")
        assert added["properties"]["kind"] == {"const": "content_present"}
        refs = four["properties"]["claims"]["items"]["oneOf"]
        refs.remove({"$ref": "#/$defs/Claim_content_present"})
        assert four == three


# ---- the step 4 wording: listed substitutions only ----------------------------------------------


def test_the_prompt_versions_are_keyed_by_step() -> None:
    assert controller.PROMPT_VERSION_BY_STEP == {3: "controller-15", 4: "controller-s4-1"}
    assert controller.PROMPT_VERSION_BY_STEP[3] == controller.PROMPT_VERSION
    assert controller.AUDIT_PROMPT_VERSION_BY_STEP == {3: "audit-6", 4: "audit-s4-1"}
    assert controller.AUDIT_PROMPT_VERSION_BY_STEP[3] == controller.AUDIT_PROMPT_VERSION


def _applied(rules: str, substitutions: tuple[tuple[str, str], ...]) -> str:
    """The substitutions applied by plain replacement: shares no code with `_s4_rules`."""
    for old, _new in substitutions:
        assert rules.count(old) == 1, old
    return functools.reduce(lambda text, pair: text.replace(*pair), substitutions, rules)


def test_the_step_4_rules_differ_from_step_3_by_exactly_the_listed_substitutions() -> None:
    """The diff pin: empty lists in 4a.3, so step 4's wording is step 3's byte for byte."""
    assert controller.S4_SUBSTITUTIONS == ()
    assert controller.AUDIT_S4_SUBSTITUTIONS == ()
    s4 = _applied(controller.SYSTEM_RULES, controller.S4_SUBSTITUTIONS)
    assert controller.SYSTEM_RULES_S4 == s4 == controller.SYSTEM_RULES
    audit = _applied(controller.AUDIT_RULES, controller.AUDIT_S4_SUBSTITUTIONS)
    assert controller.AUDIT_RULES_S4 == audit == controller.AUDIT_RULES
    assert controller.REPAIR_LEGEND_S4 == controller.REPAIR_LEGEND


def test_a_step_4_prompt_carries_the_step_4_rules_and_schema() -> None:
    rows = [controller.ListingRow(0, '1 edit "Full name"', handle=1)]
    system, _user = controller.build_prompt(
        GOAL, [], [], [], "Profile", rows, task_fence="f" * 16, screen_fence="s" * 16, step=4
    )
    assert system == [
        controller.SYSTEM_RULES_S4,
        json.dumps(controller._prompt_schema(first_turn=True, step=4), sort_keys=True),
    ]


def test_s4_rules_applies_each_substitution_once() -> None:
    assert controller._s4_rules("one two three", (("two", "2"),)) == "one 2 three"
    assert controller._s4_rules("one two", ()) == "one two"


@pytest.mark.parametrize(
    ("rules", "substitutions"),
    [
        ("one two three", (("four", "4"),)),  # missing
        ("two and two", (("two", "2"),)),  # twice
        ("one two", (("two", "2"), ("two", "2"))),  # gone after the first substitution
    ],
    ids=["missing", "twice", "consumed"],
)
def test_s4_rules_raises_when_an_old_text_is_missing_or_not_unique(
    rules: str, substitutions: tuple[tuple[str, str], ...]
) -> None:
    with pytest.raises(ValueError, match="s4_substitution"):
        controller._s4_rules(rules, substitutions)


# ---- run_task: the step and the step 4 ports agree ---------------------------------------------


class _Ledger:
    def admit(self, _key: str, /) -> Admission:
        raise AssertionError("never called in 4a.3")

    def write_ahead(self, _record: V2Record, _decision: GateDecision, /) -> bool:
        raise AssertionError("never called in 4a.3")

    def mark(self, _key: str, _phase: Phase, /) -> None:
        raise AssertionError("never called in 4a.3")

    def close(self, _key: str, _outcome: str, /) -> None:
        raise AssertionError("never called in 4a.3")


def _never_ask(_ask: Ask) -> str:
    raise AssertionError("never called in 4a.3")


def _never_check(_row: GoalRow) -> Verdict:
    raise AssertionError("never called in 4a.3")


def _never_raise(_hwnd: int) -> bool:
    raise AssertionError("never called in 4a.3")


def _never_clock() -> float:
    raise AssertionError("never called in 4a.3")


STEP4 = Step4Ports(
    ask=_never_ask,
    goal_check=_never_check,
    ledger=_Ledger(),
    raise_window=_never_raise,
    ask_wait_clock=_never_clock,
)


@pytest.mark.parametrize(("step", "with_step4"), [(4, False), (3, True)], ids=["4-none", "3-set"])
def test_run_task_refuses_a_step_that_disagrees_with_its_ports(
    step: Step, with_step4: bool
) -> None:
    world = World([decision(act("click", target=GO.id), first=True)])
    ports = world.ports()
    if with_step4:
        ports = dataclasses.replace(ports, step4=STEP4)
    with pytest.raises(StepPortsMismatchError, match="step_ports_mismatch"):
        run_task(dataclasses.replace(task(), step=step), ports)
    assert world.dispatched == []


def test_step_3_runs_with_no_step_4_ports_as_before() -> None:
    world = World([decision(act("click", target=GO.id), first=True)])
    ports = world.ports()
    assert ports.step4 is None
    outcome = run_task(task(), ports)
    assert outcome.reason != "step_ports_mismatch"
