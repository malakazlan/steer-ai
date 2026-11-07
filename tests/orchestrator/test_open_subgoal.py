"""An open-only subgoal ticks on the title its open verified (issue #47, packet open-subgoal;
schema gate REVISE 1-4, re-review 1-3; hunter OS-03/04/06/20/23): run 57's two conditions tick,
a contradicted one does not, two opens in a subgoal both run, a resumed continuation keeps
today's rule, and every open tick leaves a token-only trace record."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any

import pytest

from steerai.errors import ErrorCode
from steerai.orchestrator.contracts import Plan, Step, Subgoal, Target
from steerai.orchestrator.trace import ListTrace
from steerai.verify.predicates import (
    All,
    AnyOf,
    Appeared,
    FieldValue,
    Predicate,
    TitleMatches,
    WindowExists,
)
from tests.orchestrator.fakes import FakeConfirmer, MemoryLedger
from tests.orchestrator.test_loop import Harness, _click, _send, _type
from tests.orchestrator.test_loop import _plan as _loop_plan
from tests.orchestrator.test_open_loop import (
    IDENTITY,
    NOTEPAD_IDENTITY,
    NOTEPAD_TITLE,
    START,
    YT_TITLE,
    FakeLauncher,
    Rig,
    TwoAppDesk,
    TwoAppLauncher,
    _open,
    _two_opens,
)
from tests.orchestrator.test_takeover_loop import FakeMonitor

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


# ---- tester coverage: hunter S1/S2 rows OS-07/09/10/11/13/14/21/22 -----------------------------

GENERATED = "play drake's newest video"
"""A goal whose words do not hold the open's content: the open confirms at R2 (open_tier)."""

VALUE = FieldValue(role="Document", name_pattern="Text editor", text="hello")


def _by(trace: ListTrace) -> list[tuple[object, object]]:
    return [(t["subgoal"], t["by"]) for t in _ticks(trace)]


def _youtube_only(goal: str, step_id: str = "o1", subgoal: str = "g1") -> Plan:
    condition = TitleMatches(pattern="drake latest video")
    return Plan(
        goal=goal,
        subgoals=(Subgoal(id=subgoal, desc="results", condition=condition),),
        steps=(_open(step_id=step_id, subgoal=subgoal),),
    )


def _one_subgoal_two_opens() -> Plan:
    """Either window shows the condition, so only the every-open-verified guard (OS-06)
    keeps the first open from ticking the subgoal and skipping the second."""
    condition = AnyOf(
        parts=(WindowExists(name_pattern=YT_TITLE), WindowExists(name_pattern=NOTEPAD_TITLE))
    )
    return Plan(
        goal="search YouTube for drake latest video, then open notepad",
        subgoals=(Subgoal(id="g1", desc="both", condition=condition),),
        steps=(_open(), _notepad(step_id="o2")),
    )


def _stopped_at_the_second_open(plan: Plan) -> MemoryLedger:
    """A first run whose second open launched at R1 and never showed its title."""
    first = Rig(launcher=TwoAppLauncher(), desk=TwoAppDesk(notepad_titles=["Notepad"]))
    assert first.build().run_task(plan).evidence["detail"] == "open_unverified"
    first.ledger.set_window_ref(None)  # a new process starts with no link (as the resume test)
    return first.ledger


@pytest.mark.parametrize("kind", [All, AnyOf])
@pytest.mark.parametrize("count", [0, 1])
def test_a_vacuous_or_single_part_tree_cannot_be_built(
    kind: type[All] | type[AnyOf], count: int
) -> None:
    """OS-07 boundary: no part-less `all(...)` exists to hold vacuously on any title."""
    parts = (WindowExists(name_pattern="Notepad"),) * count
    with pytest.raises(ValueError, match=rf"^{kind.__name__} needs at least two parts$"):
        kind(parts=parts)
    padding = (WindowExists(name_pattern="x"),) * (2 - count)
    assert len(kind(parts=(*parts, *padding)).parts) == 2


def test_a_zero_step_subgoal_is_never_ticked_by_another_subgoals_open() -> None:
    """OS-07: g2 has no step; its condition holds on the window g1's open verified."""
    trace = ListTrace()
    plan = Plan(
        goal="open notepad",
        subgoals=(
            Subgoal(id="g1", desc="open", condition=WindowExists(name_pattern="Notepad")),
            Subgoal(id="g2", desc="no step", condition=WindowExists(name_pattern=NOTEPAD_TITLE)),
        ),
        steps=(_notepad(),),
    )
    result = _rig(trace).build().run_task(plan)
    assert result.ticked == {"g1"}
    assert result.status == "stopped"
    assert result.evidence == {"detail": "unfinished"}
    assert _by(trace) == [("g1", "open_title")]


@pytest.mark.parametrize(
    ("answer", "status", "ticked"),
    [
        ("yes", "done", {"g2"}),
        ("no", "blocked", set()),
        ("killed", "blocked", set()),
        ("later", "paused", set()),
    ],
)
def test_a_continuation_open_with_generated_content_ticks_only_once_confirmed(
    answer: str, status: str, ticked: set[str]
) -> None:
    """OS-09: content not in the goal confirms at R2 in a continuation too, the tier unchanged
    by the tick rule; only a confirmed, verified open ticks; a no, a kill or silence never."""
    trace = ListTrace()
    rig = _rig(trace)
    rig.confirmer = FakeConfirmer(answers=[answer])
    session = rig.build()
    assert session.run_task(_notepad_only()).ticked == {"g1"}
    result = session.run_task(_youtube_only(GENERATED, "p2.o1", "g2"), continuation=True)
    assert [a.risk for a in rig.confirmer.asked] == ["R2"]
    assert result.results[0].evidence["risk"] == "R2"
    assert result.status == status
    assert result.ticked == ticked
    assert _by(trace) == [("g1", "open_title")] + [("g2", "open_title") for _ in ticked]


@pytest.mark.parametrize(
    "condition",
    [
        VALUE,
        All(parts=(WindowExists(name_pattern=NOTEPAD_TITLE), VALUE)),
        AnyOf(parts=(WindowExists(name_pattern=NOTEPAD_TITLE), VALUE)),
    ],
)
def test_an_open_never_ticks_a_value_subgoal(condition: Predicate) -> None:
    """OS-10: a value is the type step's doing (FV-01/03), bare or nested; the AnyOf holds in
    the new window by its title part, so only the value guard keeps it unticked."""
    trace = ListTrace()
    plan = Plan(
        goal="open notepad",
        subgoals=(Subgoal(id="g1", desc="typed", condition=condition),),
        steps=(_notepad(),),
    )
    result = _rig(trace).build().run_task(plan)
    assert result.results[0].ok is True
    assert result.ticked == set()
    assert _ticks(trace) == []


@dataclass
class OtherImageDesk(TwoAppDesk):
    """The launched window shows the title but belongs to another program."""

    def identify(self, hwnd: int) -> dict[str, Any] | None:
        found = super().identify(hwnd)
        return None if found is None else {**found, "image": "calc.exe"}


def _unverified_rig(case: str, trace: ListTrace) -> Rig:
    if case == "title_never_matches":
        desk = TwoAppDesk(notepad_titles=["Notepad"])
        return Rig(launcher=TwoAppLauncher(), desk=desk, trace=trace)
    if case == "image_mismatch":
        return Rig(launcher=TwoAppLauncher(), desk=OtherImageDesk(), trace=trace)
    launcher = FakeLauncher(result_hwnd=None, detail=case)
    return Rig(launcher=launcher, desk=TwoAppDesk(), trace=trace)


@pytest.mark.parametrize(
    ("case", "detail"),
    [
        ("title_never_matches", "open_unverified"),
        ("image_mismatch", "open_unverified"),
        ("no_new_window", "no_new_window"),
        ("several_new_windows", "several_new_windows"),
    ],
)
def test_an_unverified_open_stops_before_any_tick(case: str, detail: str) -> None:
    """OS-11: the condition would hold on the code-owned title; `_after_open` stops first."""
    trace = ListTrace()
    result = _unverified_rig(case, trace).build().run_task(_notepad_only())
    assert result.status == "stopped"
    assert result.evidence["detail"] == detail
    assert result.ticked == set()
    assert _ticks(trace) == []


def test_a_rebound_first_open_and_a_fresh_second_tick_only_after_the_second() -> None:
    """OS-13: s1 rebound by identity, s2 launched; two opens keep the live check (R1)."""
    trace = ListTrace()
    rig = _rig(trace)
    rig.ledger.record_open(_open(), IDENTITY)
    result = rig.build().run_task(_one_subgoal_two_opens())
    assert [r.gate for r in result.results] == ["done_already", "approve"]
    assert [call[0].app for call in rig.launcher.calls] == ["notepad"]
    assert result.status == "done", result
    assert _by(trace) == [("g1", "open_window")]


def test_a_first_open_whose_rebind_fails_stops_with_no_tick() -> None:
    """OS-13: the recorded window's process start differs: window_gone, nothing launched."""
    trace = ListTrace()
    rig = _rig(trace)
    rig.ledger.record_open(_open(), {**IDENTITY, "start": 99})
    result = rig.build().run_task(_one_subgoal_two_opens())
    assert result.status == "stopped"
    assert result.evidence["detail"] == "window_gone"
    assert [r.step_id for r in result.results] == ["o1"]
    assert rig.launcher.calls == []
    assert result.ticked == set()
    assert _ticks(trace) == []


@pytest.mark.parametrize(
    ("answer", "gate", "status"), [("no", "block", "blocked"), ("later", "pending", "paused")]
)
def test_a_declined_reconfirm_of_the_second_open_ticks_nothing(
    answer: str, gate: str, status: str
) -> None:
    """OS-14: the second open launched unverified at R1; the resume asks at R2 and the person
    says no or nothing: the rebound first open never ticks the subgoal alone."""
    ledger = _stopped_at_the_second_open(_one_subgoal_two_opens())
    trace = ListTrace()
    confirmer = FakeConfirmer(answers=[answer])
    rig = Rig(
        launcher=TwoAppLauncher(),
        desk=TwoAppDesk(),
        ledger=ledger,
        confirmer=confirmer,
        trace=trace,
    )
    result = rig.build().run_task(_one_subgoal_two_opens())
    assert [r.gate for r in result.results] == ["done_already", gate]
    assert [(a.risk, a.previous_attempt) for a in confirmer.asked] == [
        ("R2", "delivered_unverified")
    ]
    assert rig.launcher.calls == []
    assert result.status == status
    assert result.ticked == set()
    assert _ticks(trace) == []


def test_a_declined_reconfirm_leaves_the_second_subgoal_unticked() -> None:
    """OS-14, one open per subgoal: g1's rebound open ticks it (R5); g2 never."""
    ledger = _stopped_at_the_second_open(_two_opens())
    trace = ListTrace()
    rig = Rig(
        launcher=TwoAppLauncher(),
        desk=TwoAppDesk(),
        ledger=ledger,
        confirmer=FakeConfirmer(answers=["no"]),
        trace=trace,
    )
    result = rig.build().run_task(_two_opens())
    assert result.status == "blocked"
    assert result.ticked == {"g1"}
    assert _by(trace) == [("g1", "open_title")]


def test_a_kill_during_the_title_wait_blocks_with_no_tick() -> None:
    """OS-21: the kill ends the title wait; nothing ticks and no tick is traced."""
    trace = ListTrace()
    rig = _rig(trace)
    launcher = rig.launcher

    class KillAfterLaunch(FakeMonitor):
        def kill_requested(self) -> bool:
            return bool(launcher.calls)

    rig.monitor = KillAfterLaunch(clock=rig.clock, last_user=-100.0)
    result = rig.build().run_task(_notepad_only())
    assert len(launcher.calls) == 1
    assert result.status == "blocked"
    assert result.evidence["detail"] == "killed"
    assert result.ticked == set()
    assert _ticks(trace) == []
    assert all("ticked_by" not in r.evidence for r in result.results)


def test_a_boundary_refusal_before_the_launch_ticks_nothing() -> None:
    """OS-21: the monitor is lost at the boundary; nothing launched, nothing ticked."""
    trace = ListTrace()
    rig = _rig(trace)
    rig.monitor = FakeMonitor(clock=rig.clock, last_user=-100.0, alive=False)
    result = rig.build().run_task(_notepad_only())
    assert rig.launcher.calls == []
    assert result.status == "stopped"
    assert result.code is ErrorCode.GATE_BLOCKED
    assert result.evidence["detail"] == "monitor_lost"
    assert result.ticked == set()
    assert _ticks(trace) == []


@pytest.mark.parametrize(("titles", "ticked"), [([NOTEPAD_TITLE], {"g1"}), (["Notepad"], set())])
def test_after_an_unverified_r1_launch_only_a_verified_relaunch_ticks(
    titles: list[str], ticked: set[str]
) -> None:
    """OS-22: the R1 launch never showed its title; the resume confirms at R2 and launches
    again; only a relaunch that verifies ticks, by its title."""
    first = Rig(launcher=TwoAppLauncher(), desk=TwoAppDesk(notepad_titles=["Notepad"]))
    assert first.build().run_task(_notepad_only()).evidence["detail"] == "open_unverified"
    trace = ListTrace()
    rig = Rig(
        launcher=TwoAppLauncher(),
        desk=TwoAppDesk(notepad_titles=titles),
        ledger=first.ledger,
        trace=trace,
    )
    session = rig.build()
    result = session.run_task(_notepad_only())
    assert [(a.risk, a.previous_attempt) for a in rig.confirmer.asked] == [
        ("R2", "delivered_unverified")
    ]
    assert len(rig.launcher.calls) == 1
    assert result.ticked == ticked
    assert _by(trace) == [("g1", "open_title") for _ in ticked]
    assert session.hwnd == (0x40 if ticked else START.hwnd)


# ---- every tick site writes its token record (implementation review item 2, R6) ---------------

TICK_KEYS = {"kind", "task_id", "subgoal", "by"}


def _only_tick(trace: ListTrace) -> dict[str, object]:
    (tick,) = _ticks(trace)
    assert set(tick) == TICK_KEYS, "tokens only: no title, pattern or content"
    return tick


def test_a_start_tick_is_traced() -> None:
    h = Harness()
    plan = Plan(
        goal="open the chat",
        subgoals=(
            Subgoal(id="g0", desc="chat open", condition=TitleMatches(pattern="Amir Khan - Chat")),
        ),
        steps=(replace(_click(step_id="o1"), subgoal="g0"),),
    )
    assert h.session.run_task(plan).ticked == {"g0"}
    tick = _only_tick(h.trace)
    assert (tick["subgoal"], tick["by"]) == ("g0", "start")


def test_a_condition_tick_is_traced() -> None:
    h = Harness()
    assert h.session.run_task(_loop_plan(_type(), _send())).ticked == {"g1"}
    tick = _only_tick(h.trace)
    assert (tick["subgoal"], tick["by"]) == ("g1", "condition")


def test_a_done_already_tick_is_traced() -> None:
    trace = ListTrace()
    rig = _rig(trace)
    plan = Plan(
        goal="search YouTube for drake latest video",
        subgoals=(Subgoal(id="g7", desc="other", condition=TitleMatches(pattern="never")),),
        steps=(
            replace(
                _click(step_id="c1", risk="R2", expect=TitleMatches(pattern="x")), subgoal="g7"
            ),
        ),
    )
    session = rig.build()
    rig.ledger.executions[rig.ledger.key_for(plan.steps[0])] = {
        "step_id": "c1",
        "phase": "verified",
        "outcome": "delivered_verified",
    }
    assert session.run_task(plan).ticked == {"g7"}
    tick = _only_tick(trace)
    assert (tick["subgoal"], tick["by"]) == ("g7", "done_already")


def test_two_opens_tick_by_the_live_window_and_trace_it() -> None:
    """Two opens keep the live in-window check (gate item 4): the last one's window shows it."""
    trace = ListTrace()
    plan = Plan(
        goal="search YouTube for drake latest video, then open notepad",
        subgoals=(Subgoal(id="g1", desc="both", condition=TitleMatches(pattern="Notepad")),),
        steps=(_open(), _notepad(step_id="o2")),
    )
    assert _rig(trace).build().run_task(plan).ticked == {"g1"}
    tick = _only_tick(trace)
    assert (tick["subgoal"], tick["by"]) == ("g1", "open_window")
