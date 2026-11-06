"""An open-only subgoal ticks on the title its open verified (issue #47, packet open-subgoal;
schema gate REVISE 1-4, re-review 1-3; hunter OS-03/04/06/20/23): run 57's two conditions tick,
a contradicted one does not, two opens in a subgoal both run, a resumed continuation keeps
today's rule, and every open tick leaves a token-only trace record."""

from __future__ import annotations

import json

from steerai.orchestrator.contracts import Plan, Step, Subgoal, Target
from steerai.orchestrator.trace import ListTrace
from steerai.verify.predicates import Appeared, TitleMatches, WindowExists
from tests.orchestrator.test_loop import _click
from tests.orchestrator.test_open_loop import (
    NOTEPAD_IDENTITY,
    Rig,
    TwoAppDesk,
    TwoAppLauncher,
    _open,
)

RESULTS_DOC = Appeared(
    role="Document", name_pattern="drake latest video", decoration="youtube", replaces=True
)


def _notepad(step_id: str = "s1", subgoal: str = "g1") -> Step:
    return Step(
        id=step_id,
        subgoal=subgoal,
        verb="open",
        target=Target(role="App", name="notepad"),
        risk="R1",
    )


def _run_57() -> Plan:
    """Run 57's two open subgoals, conditions verbatim (PR #46 acceptance, planner-10)."""
    return Plan(
        goal="open notepad, search YouTube for drake latest video",
        subgoals=(
            Subgoal(
                id="g1", desc="Notepad is open", condition=WindowExists(name_pattern="Notepad")
            ),
            Subgoal(id="g2", desc="YouTube shows results", condition=RESULTS_DOC),
        ),
        steps=(_notepad(), _open(step_id="s2", subgoal="g2")),
    )


def _rig(trace: ListTrace | None = None) -> Rig:
    rig = Rig(launcher=TwoAppLauncher(), desk=TwoAppDesk())
    rig.trace = trace
    return rig


def test_run_57s_open_subgoals_both_tick() -> None:
    result = _rig().build().run_task(_run_57())
    assert result.status == "done", result
    assert result.ticked == {"g1", "g2"}


def test_a_condition_the_opened_window_contradicts_does_not_tick() -> None:
    plan = Plan(
        goal="open gmail",
        subgoals=(Subgoal(id="g1", desc="inbox", condition=TitleMatches(pattern="Gmail")),),
        steps=(_notepad(),),
    )
    result = _rig().build().run_task(plan)
    assert result.ticked == set()
    assert result.status != "done"


def test_two_opens_in_one_subgoal_both_run() -> None:
    """OS-06: the first open's window must not tick the subgoal before the second opens."""
    plan = Plan(
        goal="search YouTube for drake latest video, then open notepad",
        subgoals=(Subgoal(id="g1", desc="both", condition=TitleMatches(pattern="YouTube")),),
        steps=(_open(), _notepad(step_id="o2")),
    )
    result = _rig().build().run_task(plan)
    assert [r.step_id for r in result.results] == ["o1", "o2"]


def test_the_tick_is_traced_as_a_token_with_no_title_or_pattern() -> None:
    """OS-20/23: a separate record, written after the step's own; no content."""
    trace = ListTrace()
    _rig(trace).build().run_task(_run_57())
    ticks = [json.loads(line) for line in trace.lines if '"kind": "tick"' in line]
    assert [(t["subgoal"], t["by"]) for t in ticks] == [("g1", "open_title"), ("g2", "open_title")]
    blob = "\n".join(trace.lines)
    assert "drake" not in blob.casefold()
    assert "Notepad" not in "".join(json.dumps(t) for t in ticks)


def _ticks(trace: ListTrace) -> list[dict[str, object]]:
    return [json.loads(line) for line in trace.lines if '"kind": "tick"' in line]


def _notepad_only(step_id: str = "s1") -> Plan:
    return Plan(
        goal="open notepad",
        subgoals=(Subgoal(id="g1", desc="open", condition=WindowExists(name_pattern="Notepad")),),
        steps=(_notepad(step_id=step_id),),
    )


def test_a_window_gone_at_the_tick_is_traced_with_its_reason() -> None:
    """Gate change 3: the identity refusal is recorded, never swallowed."""
    trace = ListTrace()
    rig = _rig(trace)
    desk = rig.desk
    assert isinstance(desk, TwoAppDesk)
    real, reads = desk.identify, {"n": 0}

    def flaky(hwnd: int) -> dict[str, object] | None:
        reads["n"] += 1
        # the gate's read and the open's own read see the window; the tick's does not
        return real(hwnd) if reads["n"] <= 2 else None

    desk.identify = flaky  # type: ignore[method-assign]  # the window closes after it verified
    assert rig.build().run_task(_notepad_only()).ticked == set()
    assert [(t["by"], t.get("detail")) for t in _ticks(trace)] == [("none", "window_gone")]


def test_a_resumed_continuation_with_no_verb_record_keeps_todays_rule() -> None:
    """Gate change 1: a continuation link in a new process does not know what the subgoal held
    in earlier plans; the live in-window check decides ("Notepad" is not the full title)."""
    trace = ListTrace()
    result = _rig(trace).build().run_task(_notepad_only("p2.s1"), continuation=True)
    assert result.ticked == set()
    assert all(t["by"] != "open_title" for t in _ticks(trace))


def test_a_subgoal_that_held_another_step_in_an_earlier_plan_never_ticks_on_an_open() -> None:
    """OS-03: plan 1 gave g1 an open and a click; the continuation gives it only an open."""
    rig = _rig()
    session = rig.build()
    first = Plan(
        goal="open notepad",
        subgoals=(Subgoal(id="g1", desc="open", condition=TitleMatches(pattern="Chat")),),
        steps=(_notepad(), _click(step_id="c1", expect=TitleMatches(pattern="Amir Khan - Chat"))),
    )
    assert session.run_task(first).ticked == set()
    again = session.run_task(_notepad_only("p2.s1"), continuation=True)
    assert again.ticked == set()


def test_a_rebound_open_on_resume_ticks_by_its_recomputed_title() -> None:
    """Gate item 3 / R5: the earlier run verified the window; its identity still holds."""
    trace = ListTrace()
    rig = _rig(trace)
    rig.ledger.record_open(_notepad(), {**NOTEPAD_IDENTITY, "title": "Untitled - Notepad"})
    result = rig.build().run_task(_notepad_only())
    assert result.results[0].gate == "done_already"
    assert result.ticked == {"g1"}
    assert [t["by"] for t in _ticks(trace)] == ["open_title"]
