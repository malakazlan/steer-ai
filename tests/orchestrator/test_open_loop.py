"""The loop's `open` (packet open-multiwindow-2026-11-06, scope item 4; schema gate P1-P8;
hunter OW-03/06/09/10/11/23/33): no element to resolve, the gate on the code-owned title, the
window list right before the launch, adoption of the launcher's own window only once its live
title matches, a failed or unverified open stops the task, the ledger records the window at any
tier and a resume re-binds by identity, never by title."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from steerai.action.dispositions import ActionResult, Delivery
from steerai.action.dispositions import Step as ActionStep
from steerai.errors import ErrorCode
from steerai.orchestrator.contracts import Plan, Step, Subgoal, Target
from steerai.orchestrator.gate import Gate
from steerai.orchestrator.loop import Session
from steerai.probes.windows import WindowInfo
from steerai.verify.predicates import TitleMatches, WindowExists
from tests.orchestrator.fakes import FakeConfirmer, FakeExecutor, FakeLive, FakeWorld, MemoryLedger
from tests.orchestrator.test_loop import _click

YT_TITLE = "drake latest video - YouTube - Google Chrome"
START = WindowInfo(hwnd=0x10, title="Program Manager", pid=900)
NEW = WindowInfo(hwnd=0x30, title="New Tab - Google Chrome", pid=500)
IDENTITY = {"hwnd": 0x30, "pid": 500, "start": 7, "image": "chrome.exe", "title": YT_TITLE}


@dataclass
class FakeLauncher:
    result_hwnd: int | None = 0x30
    detail: str = "no_new_window"
    calls: list[tuple[Any, list[int]]] = field(default_factory=list)

    def execute(self, action: Any, windows_before: Any) -> ActionResult:
        self.calls.append((action, [w.hwnd for w in windows_before]))
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
        return None if self.identity is None else {**self.identity, "hwnd": hwnd}

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
        )
        return self.session


def _open(text: str = "drake latest video", step_id: str = "o1") -> Step:
    return Step(
        id=step_id,
        subgoal="g1",
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
