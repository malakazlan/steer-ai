"""The loop's per-step window choice (packet step-window-2026-11-07; schema gate REVISE 7 and
rulings on hunter SW-01..42): each step's window comes from plan order, is re-bound by recorded
identity before the action key, and is read again right before dispatch."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from steerai.errors import ErrorCode
from steerai.orchestrator.contracts import Plan, Step, Subgoal
from steerai.verify.predicates import TitleMatches
from tests.orchestrator.test_loop import _click
from tests.orchestrator.test_open_loop import Rig, _open

START_ID = {"hwnd": 0x10, "pid": 600, "start": 11, "image": "notepad.exe"}
OPENED_ID = {"hwnd": 0x30, "pid": 500, "start": 7, "image": "chrome.exe"}


@dataclass
class Windows:
    """Two live windows: the start window (Notepad) and the one `o1` opened (Chrome)."""

    live: dict[int, dict[str, Any]] = field(
        default_factory=lambda: {
            0x10: {**START_ID, "visible": True, "root": True, "minimized": False},
            0x30: {**OPENED_ID, "visible": True, "root": True, "minimized": False},
        }
    )
    foreground: int = 0x10

    def identify(self, hwnd: int) -> dict[str, Any] | None:
        found = self.live.get(hwnd)
        return None if found is None else dict(found)


def _rig(windows: Windows | None = None) -> tuple[Rig, Windows]:
    win = windows or Windows()
    rig = Rig()
    rig.ledger.record_open(_open(), {**OPENED_ID, "title": "x"})
    rig.ledger.start_record = dict(START_ID)
    rig.desk.identify = win.identify  # type: ignore[method-assign]
    session = rig.build()
    session._foreground = lambda: win.foreground
    return rig, win


def _step(step_id: str, subgoal: str = "g1", window: str | None = None, **kw: Any) -> Step:
    return replace(_click(step_id=step_id, **kw), subgoal=subgoal, window=window)


def _plan(*steps: Step, subgoals: tuple[str, ...] = ("g1",)) -> Plan:
    return Plan(
        goal="g",
        subgoals=tuple(
            Subgoal(id=s, desc="d", condition=TitleMatches(pattern=f"never {s}")) for s in subgoals
        ),
        steps=steps,
    )


def _hwnds(rig: Rig) -> list[int]:
    return [o["hwnd"] for o in rig.world.observed]


def test_a_step_selects_an_opened_window_and_the_next_ones_stay_there() -> None:
    rig, _win = _rig()
    session = rig.session
    assert session is not None
    result = session.run_task(_plan(_step("c1", window="opened:o1"), _step("c2")))
    assert [r.evidence.get("window") for r in result.results] == ["selected", None]
    assert session.hwnd == 0x30
    assert rig.ledger.window_ref == "opened:o1"
    assert set(_hwnds(rig)[1:]) == {0x30}, "every observe after the start one is of the opened"


def test_start_goes_back_to_the_bound_start_window() -> None:
    rig, _win = _rig()
    session = rig.session
    assert session is not None
    session.run_task(_plan(_step("c1", window="opened:o1"), _step("c2", window="start")))
    assert session.hwnd == 0x10
    assert rig.ledger.window_ref is None


def test_the_same_step_in_two_windows_has_two_keys() -> None:
    """OW-04 / SW-03: the selection comes before the action key."""
    rig, _win = _rig()
    step = _step("c1", window="opened:o1")
    start_key = rig.ledger.key_for(step)
    session = rig.session
    assert session is not None
    seen: list[str] = []
    key_for = rig.ledger.key_for

    def spy(s: Step) -> str:
        seen.append(key_for(s))
        return seen[-1]

    rig.ledger.key_for = spy  # type: ignore[method-assign,assignment]
    session.run_step(step)
    assert seen
    assert start_key not in seen


def test_windows_follow_plan_order_when_a_window_step_is_skipped() -> None:
    """SW-01: c1 (window start) is skipped because g1 is ticked; c2 still acts in start."""
    rig, win = _rig()
    win.foreground = 0x30
    tick = _step("c0", window="opened:o1", risk="R2")
    rig.ledger.set_window_ref("opened:o1")
    rig.ledger.executions[rig.ledger.key_for(tick)] = {
        "step_id": "c0",
        "phase": "verified",
        "outcome": "delivered_verified",
    }
    rig.ledger.set_window_ref(None)
    session = rig.session
    assert session is not None
    plan = _plan(
        tick,
        _step("c1", window="start"),
        _step("c2", subgoal="g2"),
        subgoals=("g1", "g2"),
    )
    result = session.run_task(plan)
    assert [r.step_id for r in result.results] == ["c0", "c2"], "c1 skipped (g1 ticked)"
    assert result.results[1].evidence.get("window") == "selected"
    assert session.hwnd == 0x10, "c2 acts in start, the window of plan order"


def test_a_subgoal_acted_on_in_another_window_never_ticks_at_start() -> None:
    """SW-02: its condition holds in the start window, but its step acts in the opened one."""
    rig, _win = _rig()
    session = rig.session
    assert session is not None
    holds = Plan(
        goal="g",
        subgoals=(Subgoal(id="g1", desc="d", condition=TitleMatches(pattern="Amir")),),
        steps=(_step("c1", window="opened:o1"),),
    )
    result = session.run_task(holds)
    assert len(result.results) == 1, "the step ran: nothing ticked at start"


def _stopped_with(rig: Rig, step: Step) -> Any:
    session = rig.session
    assert session is not None
    result = session.run_step(step)
    assert result.ok is False
    assert result.action is None
    assert rig.semantic is not None
    assert rig.semantic.calls == []
    return result


def test_a_window_that_is_no_longer_visible_or_root_is_gone() -> None:
    for field_name in ("visible", "root"):
        win = Windows()
        win.live[0x30][field_name] = False
        rig, _ = _rig(win)
        result = _stopped_with(rig, _step("c1", window="opened:o1"))
        assert (result.code, result.evidence["detail"]) == (
            ErrorCode.ELEMENT_NOT_FOUND,
            "window_gone",
        )


def test_an_unreadable_process_never_matches() -> None:
    """SW-13: an unreadable start time read twice would compare equal."""
    win = Windows()
    win.live[0x30]["start"] = None
    rig, _ = _rig(win)
    rig.ledger.record_open(_open(), {**OPENED_ID, "start": None, "title": "x"})
    result = _stopped_with(rig, _step("c1", window="opened:o1"))
    assert result.evidence["detail"] == "identity_unreadable"


def test_a_minimized_window_is_not_selected() -> None:
    win = Windows()
    win.live[0x30]["minimized"] = True
    rig, _ = _rig(win)
    result = _stopped_with(rig, _step("c1", window="opened:o1"))
    assert (result.code, result.evidence["detail"]) == (
        ErrorCode.ELEMENT_NOT_ACTIONABLE,
        "minimized",
    )


def test_two_window_records_for_one_step_id_are_ambiguous() -> None:
    """SW-04: a replaced plan's `s1` and the current one's both recorded a window."""
    rig, _ = _rig()
    rig.ledger.executions["other-key"] = {
        "step_id": "o1",
        "phase": "verified",
        "outcome": "delivered_verified",
        "window": dict(OPENED_ID),
    }
    result = _stopped_with(rig, _step("c1", window="opened:o1"))
    assert result.evidence["detail"] == "window_ambiguous"


def test_start_without_a_recorded_identity_is_gone() -> None:
    rig, _ = _rig()
    rig.ledger.start_record = None
    result = _stopped_with(rig, _step("c1", window="start"))
    assert result.evidence["detail"] == "window_gone"


def test_start_on_a_uwp_frame_host_is_refused_until_measured() -> None:
    """SW-11: every UWP frame shares the frame host's pid and image."""
    win = Windows()
    win.live[0x10]["image"] = "applicationframehost.exe"
    rig, _ = _rig(win)
    rig.ledger.start_record = {**START_ID, "image": "applicationframehost.exe"}
    result = _stopped_with(rig, _step("c1", window="start"))
    assert (result.code, result.evidence["detail"]) == (
        ErrorCode.ELEMENT_NOT_ACTIONABLE,
        "frame_host",
    )


def test_the_identity_is_read_again_right_before_the_dispatch() -> None:
    """SW-16: the window changed between the selection and the act (an ask, a wait)."""
    rig, win = _rig()
    session = rig.session
    assert session is not None
    observe = rig.world.observe

    def replaced_while_observed(hwnd: int, **kwargs: Any) -> Any:
        win.live[0x30]["start"] = 99  # the handle now belongs to another process run
        return observe(hwnd, **kwargs)

    session._observer = type("Observer", (), {"observe": staticmethod(replaced_while_observed)})()
    result = session.run_step(_step("c1", window="opened:o1", risk="R1"))
    assert result.ok is False
    assert result.evidence["detail"] == "window_gone"
    assert rig.semantic is not None
    assert rig.semantic.calls == []


def test_an_r2_step_whose_window_is_not_in_front_stops_before_the_ask() -> None:
    """SW-15: no selection tap; the person must see where a send lands."""
    rig, win = _rig()
    win.foreground = 0x10
    session = rig.session
    assert session is not None
    result = session.run_step(_step("c1", window="opened:o1", risk="R2"))
    assert (result.code, result.evidence["detail"]) == (ErrorCode.FOCUS_LOST, "not_foreground")
    assert rig.confirmer.asked == []


def test_naming_the_current_window_is_accepted_and_still_checked() -> None:
    """Gate Q2: idempotent, the identity is still read."""
    win = Windows()
    win.live[0x10]["visible"] = False
    rig, _ = _rig(win)
    result = _stopped_with(rig, _step("c1", window="start"))
    assert result.evidence["detail"] == "window_gone"


def test_a_continuation_in_the_start_window_checks_its_identity_first() -> None:
    """SW-26: a link that starts in `start` is re-bound like an opened one."""
    win = Windows()
    win.live[0x10]["start"] = 99  # the start window's process was replaced
    rig, _ = _rig(win)
    session = rig.session
    assert session is not None
    result = session.run_task(_plan(_step("p2.c1")), continuation=True)
    assert result.evidence["detail"] == "window_gone"
    assert result.results == []


def test_live_title_reads_a_reference_only_while_its_identity_holds() -> None:
    """SW-36: the planner sees only windows that are still the task's."""
    win = Windows()
    win.live[0x30]["title"] = "drake - YouTube"
    win.live[0x10]["title"] = "Untitled - Notepad"
    rig, _ = _rig(win)
    session = rig.session
    assert session is not None
    assert session.live_title("opened:o1") == "drake - YouTube"
    assert session.live_title("start") == "Untitled - Notepad"
    win.live[0x30]["start"] = 99
    assert session.live_title("opened:o1") is None
    assert session.live_title("opened:o9") is None


def test_the_window_label_is_code_owned_first_and_the_title_is_capped() -> None:
    """SW-06: the app from the registry by image, the reference, then the page title."""
    win = Windows()
    win.live[0x30]["title"] = "x" * 500
    rig, _ = _rig(win)
    session = rig.session
    assert session is not None
    session.run_step(_step("c1", window="opened:o1"))
    label = session.window_label()
    assert label.startswith("chrome (opened:o1): ")
    assert len(label) <= len("chrome (opened:o1): ") + 80
    win.live[0x10]["title"] = "Untitled - Notepad"
    session.run_step(_step("c2", window="start"))
    assert session.window_label() == "notepad (start): Untitled - Notepad"


def test_an_unknown_image_shows_its_image_name() -> None:
    win = Windows()
    win.live[0x30]["image"] = "whatsapp.exe"
    rig, _ = _rig(win)
    rig.ledger.record_open(_open(), {**OPENED_ID, "image": "whatsapp.exe", "title": "x"})
    session = rig.session
    assert session is not None
    session.run_step(_step("c1", window="opened:o1"))
    assert session.window_label().startswith("whatsapp.exe (opened:o1): ")


def test_a_continuation_in_a_frame_host_start_window_still_runs() -> None:
    """Principal review of PR #35, item 1: only `window: start` is refused there (SW-11), never
    #29's continuation of a task that began in Calculator or Settings."""
    win = Windows()
    win.live[0x10]["image"] = "applicationframehost.exe"
    rig, _ = _rig(win)
    rig.ledger.start_record = {**START_ID, "image": "applicationframehost.exe"}
    session = rig.session
    assert session is not None
    result = session.run_task(_plan(_step("p2.c1")), continuation=True)
    assert result.evidence.get("detail") != "frame_host"
    assert len(result.results) == 1


def test_the_foreground_rule_leaves_the_start_windows_own_steps_alone() -> None:
    """Review item 3: no selection, no new rule on the existing send path."""
    rig, win = _rig()
    win.foreground = 0x99  # the console after a semantic R2 answer
    session = rig.session
    assert session is not None
    result = session.run_step(_step("c1", risk="R2"))
    assert result.evidence.get("detail") != "not_foreground"


def test_a_continuations_own_start_window_steps_keep_the_send_path() -> None:
    """PR #35 re-review: the continuation's start check sets the identity, not a switch."""
    rig, win = _rig()
    win.foreground = 0x99  # the console after a semantic R2 answer
    session = rig.session
    assert session is not None
    result = session.run_task(_plan(_step("p2.c1", risk="R2")), continuation=True)
    assert all(r.evidence.get("detail") != "not_foreground" for r in result.results)
    assert result.results, "the step ran"
