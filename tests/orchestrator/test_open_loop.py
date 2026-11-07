"""The loop's `open` (packet open-multiwindow-2026-11-06, scope item 4; schema gate P1-P8;
hunter OW-03/06/09/10/11/23/33): no element to resolve, the gate on the code-owned title, the
window list right before the launch, adoption of the launcher's own window only once its live
title matches, a failed or unverified open stops the task, the ledger records the window at any
tier and a resume re-binds by identity, never by title."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

import pytest

from steerai.action.dispositions import ActionResult, Delivery
from steerai.action.dispositions import Step as ActionStep
from steerai.errors import ErrorCode
from steerai.orchestrator.contracts import Plan, Step, Subgoal, Target
from steerai.orchestrator.gate import Gate
from steerai.orchestrator.loop import Session
from steerai.probes.windows import WindowInfo
from steerai.verify.predicates import All, AnyOf, TitleMatches, WindowExists
from tests.orchestrator.fakes import FakeConfirmer, FakeExecutor, FakeLive, FakeWorld, MemoryLedger
from tests.orchestrator.test_loop import _click

YT_TITLE = "drake latest video - YouTube - Google Chrome"
START = WindowInfo(hwnd=0x10, title="Program Manager", pid=900)
NEW = WindowInfo(hwnd=0x30, title="New Tab - Google Chrome", pid=500)
IDENTITY = {"hwnd": 0x30, "pid": 500, "start": 7, "image": "chrome.exe", "title": YT_TITLE}
LIVE = {"visible": True, "root": True, "minimized": False, "cloaked": False}
"""What a live window reads besides its identity (step-window SW-12/14)."""


@dataclass
class FakeLauncher:
    result_hwnd: int | None = 0x30
    detail: str = "no_new_window"
    calls: list[tuple[Any, list[int]]] = field(default_factory=list)
    after_launch: Any = None  # Any: a test hook run right after the launch

    def execute(self, action: Any, windows_before: Any) -> ActionResult:
        self.calls.append((action, [w.hwnd for w in windows_before]))
        if self.after_launch is not None:
            self.after_launch()
        if self.result_hwnd is None:
            return ActionResult(
                verb="open",
                delivery=Delivery.DELIVERY_UNCERTAIN,
                steps=[ActionStep(label="launch", mechanism="launch", outcome="succeeded")],
                code=ErrorCode.VERIFY_TIMEOUT,
                evidence={"detail": self.detail, "launched": "chrome.exe"},
            )
        return ActionResult(
            verb="open",
            delivery=Delivery.DELIVERED_UNVERIFIED,
            steps=[ActionStep(label="launch", mechanism="launch", outcome="succeeded")],
            code=ErrorCode.VERIFY_TIMEOUT,
            evidence={"hwnd": self.result_hwnd, "launched": "chrome.exe"},
        )


@dataclass
class Desk:
    """Window titles and identities as the OS would report them."""

    titles: list[str] = field(default_factory=lambda: ["New Tab - Google Chrome", YT_TITLE])
    identity: dict[str, Any] | None = field(default_factory=lambda: dict(IDENTITY))
    windows_now: list[WindowInfo] = field(default_factory=lambda: [START])
    reads: int = 0

    def title_of(self, hwnd: int) -> str:
        assert hwnd == 0x30, "only the launcher's own window is read"
        self.reads += 1
        return self.titles[min(self.reads - 1, len(self.titles) - 1)]

    def identify(self, hwnd: int) -> dict[str, Any] | None:
        return None if self.identity is None else {**LIVE, **self.identity, "hwnd": hwnd}

    def windows(self) -> list[WindowInfo]:
        return list(self.windows_now)


@dataclass
class Rig:
    world: FakeWorld = field(default_factory=FakeWorld)
    launcher: FakeLauncher = field(default_factory=FakeLauncher)
    desk: Desk = field(default_factory=Desk)
    confirmer: FakeConfirmer = field(default_factory=FakeConfirmer)
    ledger: MemoryLedger = field(default_factory=lambda: MemoryLedger(task_id="t", started_at=1.0))
    clock: dict[str, float] = field(default_factory=lambda: {"t": 0.0})
    semantic: FakeExecutor | None = None
    session: Session | None = None
    monitor: Any = None
    trace: Any = None  # Any: a ListTrace or None

    def build(self) -> Session:
        self.semantic = FakeExecutor(self.world, "semantic")
        self.session = Session(
            task_id="t",
            hwnd=START.hwnd,
            observer=self.world,
            semantic=self.semantic,
            physical=FakeExecutor(self.world, "physical"),
            gate=Gate(confirmer=self.confirmer, own_pid=4242, task_id="t"),
            ledger=self.ledger,
            live=FakeLive(self.world),
            windows=self.desk.windows,
            monotonic=lambda: self.clock["t"],
            sleep=lambda s: self.clock.__setitem__("t", self.clock["t"] + s),
            verify_deadline_s=0.5,
            launcher=self.launcher,
            window_title=self.desk.title_of,
            identify=self.desk.identify,
            foreground=lambda: 0x30,
            monitor=self.monitor,
            trace=self.trace,
        )
        return self.session


def _open(text: str = "drake latest video", step_id: str = "o1", subgoal: str = "g1") -> Step:
    return Step(
        id=step_id,
        subgoal=subgoal,
        verb="open",
        target=Target(role="App", name="chrome"),
        risk="R1",
        params={"site": "youtube_search", "text": text},
    )


def _plan(*steps: Step, goal: str = "search YouTube for drake latest video") -> Plan:
    return Plan(
        goal=goal,
        subgoals=(Subgoal(id="g1", desc="results", condition=WindowExists(name_pattern=YT_TITLE)),),
        steps=steps,
    )


def test_a_verified_open_adopts_the_launchers_window_and_ticks_its_subgoal() -> None:
    rig = Rig()
    session = rig.build()
    result = session.run_task(_plan(_open()))
    assert result.status == "done", result
    step = result.results[0]
    assert step.ok is True
    assert step.gate == "approve"
    assert step.evidence["window"] == "opened"
    assert step.evidence["foreground_after_open"] is True
    assert step.label_kind == "t1"
    assert session.hwnd == 0x30
    assert rig.ledger.opened_window(_open()) is not None or rig.ledger.window_ref == "opened:o1"
    assert rig.ledger.window_ref == "opened:o1"
    action, before_hwnds = rig.launcher.calls[0]
    assert (action.app, action.site, action.text) == (
        "chrome",
        "youtube_search",
        "drake latest video",
    )
    assert before_hwnds == [START.hwnd]
    assert rig.desk.reads >= 2, "the live title is re-read until it matches (OW-06)"


def test_an_open_whose_title_never_matches_stops_the_task_in_the_old_window() -> None:
    rig = Rig(desk=Desk(titles=["New Tab - Google Chrome"]))
    session = rig.build()
    result = session.run_task(
        _plan(_open(), _click(step_id="c1", expect=TitleMatches(pattern="x")))
    )
    assert result.status == "stopped"
    assert result.evidence["detail"] == "open_unverified"
    assert len(result.results) == 1, "nothing ran after the failed open (OW-03)"
    assert session.hwnd == START.hwnd
    assert rig.ledger.window_ref is None


@pytest.mark.parametrize("cloaked", [True, None])
def test_an_open_whose_window_dwm_hides_or_cannot_read_is_unverified(cloaked: bool | None) -> None:
    """Issue #48 (CU-09): the title matched, but the window is cloaked or its cloak unreadable."""
    rig = Rig(desk=Desk(identity={**IDENTITY, "cloaked": cloaked}))
    session = rig.build()
    result = session.run_task(_plan(_open()))
    assert result.status == "stopped"
    assert result.evidence["detail"] == "open_unverified"
    assert session.hwnd == START.hwnd


def test_a_launch_with_no_window_stops_the_task() -> None:
    rig = Rig(launcher=FakeLauncher(result_hwnd=None))
    session = rig.build()
    result = session.run_task(
        _plan(_open(), _click(step_id="c1", expect=TitleMatches(pattern="x")))
    )
    assert result.status == "stopped"
    assert result.evidence["detail"] == "no_new_window"
    assert len(result.results) == 1
    assert session.hwnd == START.hwnd


def test_generated_content_confirms_at_r2_and_shows_the_host() -> None:
    rig = Rig()
    session = rig.build()
    result = session.run_task(_plan(_open(), goal="play drake's newest video"))
    assert result.results[0].gate == "confirm"
    ask = rig.confirmer.asked[0]
    assert ask.risk == "R2"
    assert ask.params["opens"] == "www.youtube.com"
    assert result.status == "done"


def test_the_window_list_is_taken_right_before_the_launch() -> None:
    """OW-10 / P4: a window the person opened during the ask is in the before list, so it can
    never be mistaken for the launched one."""
    rig = Rig()
    person_tab = WindowInfo(hwnd=0x22, title="New Tab - Google Chrome", pid=500)

    def answer(_ask: Any) -> None:
        rig.desk.windows_now.append(person_tab)

    rig.confirmer.on_ask = answer
    session = rig.build()
    session.run_task(_plan(_open(), goal="play drake's newest video"))
    _action, before_hwnds = rig.launcher.calls[0]
    assert 0x22 in before_hwnds


def test_the_models_expect_on_an_open_is_never_used() -> None:
    rig = Rig()
    session = rig.build()
    step = Step(
        id="o1",
        subgoal="g1",
        verb="open",
        target=Target(role="App", name="chrome"),
        risk="R1",
        params={"site": "youtube_search", "text": "drake latest video"},
        expect=TitleMatches(pattern="anything at all"),
    )
    result = session.run_step(step)
    assert result.ok is True
    assert result.evidence["window"] == "opened"


def test_a_resume_rebinds_the_opened_window_by_identity_without_launching() -> None:
    rig = Rig()
    rig.ledger.record_open(_open(), IDENTITY)
    session = rig.build()
    result = session.run_step(_open())
    assert result.gate == "done_already"
    assert rig.launcher.calls == [], "never launched again (OW-11)"
    assert session.hwnd == 0x30
    assert rig.ledger.window_ref == "opened:o1"


def test_a_resume_whose_window_changed_identity_stops_window_gone() -> None:
    rig = Rig(desk=Desk(identity={**IDENTITY, "start": 99}))
    rig.ledger.record_open(_open(), IDENTITY)
    session = rig.build()
    result = session.run_task(
        _plan(_open(), _click(step_id="c1", expect=TitleMatches(pattern="x")))
    )
    assert result.status == "stopped"
    assert result.evidence["detail"] == "window_gone"
    assert rig.launcher.calls == []
    assert session.hwnd == START.hwnd


def test_a_step_after_an_open_is_a_different_action_than_before_it() -> None:
    rig = Rig()
    session = rig.build()
    click = _click(step_id="c1", expect=TitleMatches(pattern="Amir Khan - Chat"))
    key_before = rig.ledger.key_for(click)
    session.run_step(_open())
    assert rig.ledger.key_for(click) != key_before


# ---- tester gaps (sign-off 2026-11-06) ------------------------------------------------------

NOTEPAD_TITLE = "Untitled - Notepad"
NOTEPAD_IDENTITY = {"hwnd": 0x40, "pid": 600, "start": 9, "image": "notepad.exe"}


@dataclass
class TwoAppDesk(Desk):
    notepad_titles: list[str] = field(default_factory=lambda: [NOTEPAD_TITLE])

    def title_of(self, hwnd: int) -> str:
        if hwnd == 0x40:
            return self.notepad_titles[0]
        return YT_TITLE

    def identify(self, hwnd: int) -> dict[str, Any] | None:
        return {**(NOTEPAD_IDENTITY if hwnd == 0x40 else IDENTITY), **LIVE, "hwnd": hwnd}


@dataclass
class TwoAppLauncher(FakeLauncher):
    def execute(self, action: Any, windows_before: Any) -> ActionResult:
        self.result_hwnd = 0x40 if action.app == "notepad" else 0x30
        return super().execute(action, windows_before)


def _two_opens() -> Plan:
    notepad = Step(
        id="o2", subgoal="g2", verb="open", target=Target(role="App", name="notepad"), risk="R1"
    )
    return Plan(
        goal="search YouTube for drake latest video, then open notepad",
        subgoals=(
            Subgoal(id="g1", desc="results", condition=WindowExists(name_pattern=YT_TITLE)),
            Subgoal(id="g2", desc="notepad", condition=WindowExists(name_pattern=NOTEPAD_TITLE)),
        ),
        steps=(_open(), notepad),
    )


def test_a_resume_after_the_first_of_two_opens_rebinds_it_and_launches_only_the_second() -> None:
    first = Rig(launcher=TwoAppLauncher(), desk=TwoAppDesk(notepad_titles=["Notepad"]))
    stopped = first.build().run_task(_two_opens())
    assert stopped.status == "stopped"
    assert stopped.evidence["detail"] == "open_unverified"
    resumed = Rig(launcher=TwoAppLauncher(), desk=TwoAppDesk(), ledger=first.ledger)
    resumed.ledger.set_window_ref(None)
    session = resumed.build()
    result = session.run_task(_two_opens())
    assert result.status == "done", result
    # the second open's first attempt launched unverified: the resume confirms before it
    # launches again (a window may already be open)
    assert [r.gate for r in result.results] == ["done_already", "confirm"]
    assert [call[0].app for call in resumed.launcher.calls] == ["notepad"]
    assert session.hwnd == 0x40
    assert resumed.ledger.window_ref == "opened:o2"
    resumed.ledger.set_window_ref("opened:o1")
    assert resumed.ledger.opened_window(_two_opens().steps[1]) is not None


def test_a_kill_during_the_launchers_poll_blocks_the_task_and_adopts_nothing() -> None:
    rig = Rig(launcher=FakeLauncher(result_hwnd=None, detail="killed"))
    session = rig.build()
    result = session.run_task(
        _plan(_open(), _click(step_id="c1", expect=TitleMatches(pattern="x")))
    )
    assert result.status == "blocked"
    assert result.code is ErrorCode.GATE_BLOCKED
    assert len(result.results) == 1
    assert session.hwnd == START.hwnd
    assert rig.ledger.window_ref is None


def test_a_kill_while_the_title_is_awaited_blocks_with_nothing_adopted() -> None:
    from tests.orchestrator.test_takeover_loop import FakeMonitor  # noqa: PLC0415

    rig = Rig(desk=Desk(titles=["New Tab - Google Chrome"]))

    class KillAfterLaunch(FakeMonitor):
        def kill_requested(self) -> bool:
            return bool(rig.launcher.calls)

    rig.monitor = KillAfterLaunch(clock=rig.clock, last_user=-100.0)
    session = rig.build()
    result = session.run_task(_plan(_open()))
    assert result.status == "blocked"
    assert result.evidence["detail"] == "killed"
    assert len(rig.launcher.calls) == 1
    assert rig.desk.reads == 0, "the kill is checked before the first title read"
    assert session.hwnd == START.hwnd
    assert rig.ledger.window_ref is None


@pytest.mark.parametrize(
    ("condition", "status"),
    [
        (All(parts=(WindowExists(name_pattern=YT_TITLE), TitleMatches(pattern="YouTube"))), "done"),
        (
            AnyOf(parts=(WindowExists(name_pattern="Inbox"), WindowExists(name_pattern=YT_TITLE))),
            "done",
        ),
        (TitleMatches(pattern="drake latest"), "done"),
        (
            All(parts=(WindowExists(name_pattern=YT_TITLE), TitleMatches(pattern="Gmail"))),
            "stopped",
        ),
        (
            AnyOf(parts=(WindowExists(name_pattern="Inbox"), TitleMatches(pattern="Gmail"))),
            "stopped",
        ),
    ],
)
def test_an_opens_subgoal_holds_in_the_window_it_opened(condition: Any, status: str) -> None:
    rig = Rig(desk=Desk(titles=[YT_TITLE]))
    plan = Plan(
        goal="search YouTube for drake latest video",
        subgoals=(Subgoal(id="g1", desc="results", condition=condition),),
        steps=(_open(),),
    )
    assert rig.build().run_task(plan).status == status


# ---- principal implementation review (items 2, 3, 6) ------------------------------------------


def _two_step_plan(goal: str = "search YouTube for drake latest video") -> Plan:
    """An open and a click in one subgoal: the click is what reaches it (item 2)."""
    from steerai.verify.predicates import Appeared  # noqa: PLC0415

    return Plan(
        goal=goal,
        subgoals=(
            Subgoal(id="g1", desc="video", condition=Appeared(role="Button", name_pattern="Pause")),
        ),
        steps=(_open(), _click(step_id="c1", expect=TitleMatches(pattern="Amir Khan - Chat"))),
    )


def test_a_rebound_open_never_ticks_its_subgoal_without_a_check() -> None:
    rig = Rig()
    rig.ledger.record_open(_open(), IDENTITY)
    session = rig.build()
    result = session.run_task(_two_step_plan())
    assert [r.step_id for r in result.results] == ["o1", "c1"], "the click still runs"


def test_an_open_never_ticks_a_subgoal_that_still_has_another_step() -> None:
    """Hunter OS-02 (issue #47): the condition held in the fresh window, the open ticked and the
    click never ran, a false `done`. The click runs; a title that was already true before it is
    not its effect, so the task ends unfinished: unclear means fail."""
    rig = Rig(desk=Desk(titles=[YT_TITLE]))
    plan = Plan(
        goal="search YouTube for drake latest video",
        subgoals=(Subgoal(id="g1", desc="video", condition=TitleMatches(pattern="YouTube")),),
        steps=(_open(), _click(step_id="c1", expect=TitleMatches(pattern="Amir Khan - Chat"))),
    )
    result = rig.build().run_task(plan)
    assert [r.step_id for r in result.results] == ["o1", "c1"], "the click still runs"
    assert result.status == "stopped"
    assert result.ticked == set()


def test_an_open_subgoal_is_never_ticked_at_task_start() -> None:
    """The start window may already show the condition; the open's subgoal still runs."""
    rig = Rig()
    rig.world.rows.append((40, "Button", "Pause", (42, 90), 0))
    session = rig.build()
    result = session.run_task(_two_step_plan())
    assert result.results[0].step_id == "o1"


def test_the_new_window_check_keeps_the_scope_and_case_rules() -> None:
    """Item 3: an `appeared` in the new window goes through the verifier, not a bare scan."""
    from steerai.verify.predicates import Appeared, Within  # noqa: PLC0415

    rig = Rig()
    rig.world.rows.append((40, "Button", "pause", (42, 90), 0))
    session = rig.build()
    session.run_step(_open())
    scoped = Appeared(role="Button", name_pattern="Pause", within=Within(landmark="Nowhere"))
    assert session._holds_in_window(scoped) is False
    window_wide = Appeared(role="Button", name_pattern="Pause")
    assert session._holds_in_window(window_wide) is False, "window-wide matches keep case"


def test_a_resume_with_two_opens_keys_the_steps_between_by_the_first_window() -> None:
    rig = Rig()
    first, second = _open(step_id="o1"), _open(step_id="o2")
    click = _click(step_id="c1", expect=TitleMatches(pattern="Amir Khan - Chat"))
    rig.ledger.record_open(first, IDENTITY)
    rig.ledger.set_window_ref("opened:o1")
    click_key = rig.ledger.key_for(click)
    rig.ledger.set_window_ref(None)
    session = rig.build()
    session.run_step(first)  # rebound
    assert rig.ledger.window_ref == "opened:o1"
    assert rig.ledger.key_for(click) == click_key
    session.run_step(second)
    assert rig.ledger.window_ref == "opened:o2"
    assert rig.launcher.calls, "the second open launched"


def test_a_kill_while_the_title_loads_blocks_with_nothing_adopted() -> None:
    from tests.orchestrator.test_takeover_loop import FakeMonitor  # noqa: PLC0415

    rig = Rig(desk=Desk(titles=["New Tab - Google Chrome"]))
    monitor = FakeMonitor(clock=rig.clock)
    session = rig.build()
    session._monitor = monitor

    rig.launcher.after_launch = lambda: setattr(monitor, "kill", True)
    result = session.run_task(
        _plan(_open(), _click(step_id="c1", expect=TitleMatches(pattern="x")))
    )
    assert result.status == "blocked"
    assert result.evidence["detail"] == "killed"
    assert session.hwnd == START.hwnd
    assert rig.ledger.window_ref is None


def test_a_resume_after_an_unverified_open_asks_before_launching_again() -> None:
    """Tester gap: an earlier attempt may have opened a window; never a silent second launch."""
    rig = Rig(desk=Desk(titles=["New Tab - Google Chrome"]))
    session = rig.build()
    goal = "search YouTube for drake latest video"  # verbatim content: the first attempt is R1
    session._goal = goal
    first = session.run_step(_open())
    assert first.ok is False
    assert first.evidence["risk"] == "R1"
    rig2 = Rig(ledger=rig.ledger, confirmer=FakeConfirmer(answers=["no"]))
    resumed = rig2.build()
    resumed._goal = goal
    result = resumed.run_step(_open())
    assert rig2.confirmer.asked, "the person is asked first"
    ask = rig2.confirmer.asked[0]
    assert ask.risk == "R2"
    assert ask.previous_attempt == "delivered_unverified"
    assert rig2.launcher.calls == []
    assert result.ok is False


# ---- a continuation plan starts in the window the task reached (replan addendum item 3) ------


def _in_window_plan() -> Plan:
    return Plan(
        goal="search YouTube for drake latest video and play the first video",
        subgoals=(Subgoal(id="g2", desc="playing", condition=TitleMatches(pattern="x")),),
        steps=(replace(_click(step_id="p2.c1", expect=TitleMatches(pattern="x")), subgoal="g2"),),
    )


def test_a_continuation_rebinds_the_opened_window_by_its_reference_first() -> None:
    rig = Rig()
    rig.ledger.record_open(_open(), IDENTITY)
    rig.ledger.set_window_ref("opened:o1")  # the link's ref, restored by bind_plan
    session = rig.build()
    result = session.run_task(_in_window_plan())
    assert result.evidence.get("detail") != "window_gone", result
    assert session.hwnd == 0x30
    assert rig.launcher.calls == []
    assert rig.world.observed[0]["hwnd"] == 0x30, "the start observe is of the opened window"


def test_a_continuation_whose_window_changed_identity_stops_before_any_step() -> None:
    rig = Rig(desk=Desk(identity={**IDENTITY, "start": 99}))
    rig.ledger.record_open(_open(), IDENTITY)
    rig.ledger.set_window_ref("opened:o1")
    session = rig.build()
    result = session.run_task(_in_window_plan())
    assert result.status == "stopped"
    assert result.code is ErrorCode.ELEMENT_NOT_FOUND
    assert result.evidence["detail"] == "window_gone"
    assert result.results == []
    assert session.hwnd == START.hwnd


def test_a_continuation_whose_reference_names_no_recorded_window_stops() -> None:
    rig = Rig()
    rig.ledger.set_window_ref("opened:o9")
    result = rig.build().run_task(_in_window_plan())
    assert result.evidence["detail"] == "window_gone"
    assert result.results == []


def test_a_continuation_never_ticks_a_subgoal_at_start() -> None:
    """Hunter RP-01: a rewritten condition that already holds would end the task with nothing
    done; in a continuation only a verified step ticks."""
    rig = Rig()
    rig.ledger.record_open(_open(), IDENTITY)
    rig.ledger.set_window_ref("opened:o1")
    holds = Plan(
        goal="search YouTube for drake latest video and play the first video",
        subgoals=(Subgoal(id="g2", desc="playing", condition=TitleMatches(pattern="Amir")),),
        steps=(replace(_click(step_id="p2.c1", expect=TitleMatches(pattern="x")), subgoal="g2"),),
    )
    first = rig.build().run_task(holds)
    assert first.ticked == {"g2"}, "a first plan still ticks what holds at its start"
    assert first.results == []
    again = Rig(ledger=rig.ledger)
    result = again.build().run_task(holds, continuation=True)
    assert len(result.results) == 1, "the step ran"
    assert result.results[0].ok is False
    assert "g2" not in result.ticked


def test_a_done_already_by_another_step_ticks_nothing() -> None:
    """Hunter RP-03: a later step keyed like an earlier verified one is done_already, but its
    own subgoal is not proven by it."""
    rig = Rig()
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
    click = plan.steps[0]
    key = rig.ledger.key_for(click)
    rig.ledger.executions[key] = {
        "step_id": "c0",
        "phase": "verified",
        "outcome": "delivered_verified",
    }
    result = session.run_task(plan)
    assert result.results[0].gate == "done_already"
    assert result.results[0].evidence["done_by"] == "another_step"
    assert "g7" not in result.ticked


def test_a_done_already_by_its_own_earlier_attempt_still_ticks() -> None:
    rig = Rig()
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
    key = rig.ledger.key_for(plan.steps[0])
    rig.ledger.executions[key] = {
        "step_id": "c1",
        "phase": "verified",
        "outcome": "delivered_verified",
    }
    result = session.run_task(plan)
    assert result.results[0].evidence["done_by"] == "this_step"
    assert result.ticked == {"g7"}
