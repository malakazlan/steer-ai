"""The loop's per-step window choice (packet step-window-2026-11-07; schema gate REVISE 7 and
rulings on hunter SW-01..42): each step's window comes from plan order, is re-bound by recorded
identity before the action key, and is read again right before dispatch."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any

import pytest

from steerai.errors import ErrorCode
from steerai.orchestrator.contracts import Plan, Step, Subgoal
from steerai.orchestrator.trace import ListTrace
from steerai.verify.predicates import TitleMatches
from tests.orchestrator.test_loop import HELLO, _click, _send, _type
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


# ---- tester gaps (sign-off 2026-11-06) ------------------------------------------------------


@pytest.mark.parametrize(
    ("field_name", "changed"), [("pid", 601), ("start", 99), ("image", "other.exe")]
)
def test_any_one_identity_field_that_changed_is_a_gone_window(
    field_name: str, changed: object
) -> None:
    """SW-12/16: pid, process start and image each decide on their own."""
    win = Windows()
    win.live[0x30][field_name] = changed
    rig, _ = _rig(win)
    result = _stopped_with(rig, _step("c1", window="opened:o1"))
    assert (result.code, result.evidence["detail"]) == (
        ErrorCode.ELEMENT_NOT_FOUND,
        "window_gone",
    )


@pytest.mark.parametrize("side", ["recorded", "live"])
@pytest.mark.parametrize(("field_name", "unreadable"), [("start", None), ("image", "")])
def test_an_identity_unreadable_on_either_side_never_matches(
    side: str, field_name: str, unreadable: object
) -> None:
    """SW-13: an unreadable field in the record or in the live read is never a match."""
    win = Windows()
    rig, _ = _rig(win)
    if side == "live":
        win.live[0x30][field_name] = unreadable
    else:
        rig.ledger.record_open(_open(), {**OPENED_ID, field_name: unreadable, "title": "x"})
    result = _stopped_with(rig, _step("c1", window="opened:o1"))
    assert (result.code, result.evidence["detail"]) == (
        ErrorCode.ELEMENT_NOT_FOUND,
        "identity_unreadable",
    )


def test_a_minimized_state_that_was_not_read_is_refused() -> None:
    """SW-14: only a window read as not minimized is acted in (unclear means fail)."""
    win = Windows()
    del win.live[0x30]["minimized"]
    rig, _ = _rig(win)
    result = _stopped_with(rig, _step("c1", window="opened:o1"))
    assert (result.code, result.evidence["detail"]) == (
        ErrorCode.ELEMENT_NOT_ACTIONABLE,
        "minimized",
    )


def test_a_window_minimized_after_the_selection_stops_before_the_dispatch() -> None:
    """SW-14 through SW-16: the re-read right before the dispatch refuses it."""
    rig, win = _rig()
    session = rig.session
    assert session is not None
    observe = rig.world.observe

    def minimized_while_observed(hwnd: int, **kwargs: Any) -> Any:
        win.live[0x30]["minimized"] = True
        return observe(hwnd, **kwargs)

    session._observer = type("Observer", (), {"observe": staticmethod(minimized_while_observed)})()
    result = session.run_step(_step("c1", window="opened:o1", risk="R1"))
    assert (result.code, result.evidence["detail"]) == (
        ErrorCode.ELEMENT_NOT_ACTIONABLE,
        "minimized",
    )
    assert rig.semantic is not None
    assert rig.semantic.calls == []


def test_an_r3_step_whose_window_is_not_in_front_stops_before_the_ask() -> None:
    """SW-15: R3 like R2; the person must see where it lands."""
    rig, win = _rig()
    win.foreground = 0x10
    session = rig.session
    assert session is not None
    result = session.run_step(_step("c1", window="opened:o1", risk="R3"))
    assert (result.code, result.evidence["detail"]) == (ErrorCode.FOCUS_LOST, "not_foreground")
    assert rig.confirmer.asked == []


def test_an_r1_step_acts_in_a_window_that_is_not_in_front() -> None:
    """SW-15 boundary: below R2 nothing is asked, so the foreground is not required."""
    rig, win = _rig()
    win.foreground = 0x10
    session = rig.session
    assert session is not None
    result = session.run_step(_step("c1", window="opened:o1", risk="R1"))
    assert result.evidence.get("detail") != "not_foreground"
    assert rig.semantic is not None
    assert rig.semantic.calls != []


@pytest.mark.parametrize("gone", ["no_record", "closed"])
def test_an_opened_window_without_a_record_or_closed_is_gone(gone: str) -> None:
    """SW-29/30: a ref no open of this task verified, or a done open whose window closed."""
    win = Windows()
    ref = "opened:o9" if gone == "no_record" else "opened:o1"
    if gone == "closed":
        del win.live[0x30]
    rig, _ = _rig(win)
    result = _stopped_with(rig, _step("c1", window=ref))
    assert (result.code, result.evidence["detail"]) == (
        ErrorCode.ELEMENT_NOT_FOUND,
        "window_gone",
    )


def test_an_open_after_a_skipped_window_step_keys_in_its_plan_order_window() -> None:
    """SW-17: the open's key carries the window of plan order (start here), not the one the
    done step before it selected, so a resume reproduces it."""
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
    seen: list[tuple[str, str | None]] = []
    key_for = rig.ledger.key_for

    def spy(s: Step) -> str:
        seen.append((s.id, rig.ledger.window_ref))
        return key_for(s)

    rig.ledger.key_for = spy  # type: ignore[method-assign,assignment]  # a spy on the fake
    session = rig.session
    assert session is not None
    plan = _plan(
        tick,
        _step("c1", window="start"),
        replace(_open(step_id="o2"), subgoal="g2"),
        subgoals=("g1", "g2"),
    )
    session.run_task(plan)
    assert [ref for step_id, ref in seen if step_id == "o2"][:1] == [None]


def test_a_kill_during_the_identity_read_injects_nothing() -> None:
    """SW-23: the kill lands while the window is being checked; nothing is dispatched."""
    from tests.orchestrator.test_takeover_loop import FakeMonitor  # noqa: PLC0415

    win = Windows()
    rig = Rig()
    monitor = FakeMonitor(clock=rig.clock, last_user=-100.0)
    rig.monitor = monitor
    rig.ledger.record_open(_open(), {**OPENED_ID, "title": "x"})
    rig.ledger.start_record = dict(START_ID)

    def identify(hwnd: int) -> dict[str, Any] | None:
        monitor.kill = True
        return win.identify(hwnd)

    rig.desk.identify = identify  # type: ignore[method-assign]
    session = rig.build()
    session._foreground = lambda: win.foreground
    result = session.run_step(_step("c1", window="opened:o1"))
    assert result.ok is False
    assert result.evidence["detail"] == "killed"
    assert rig.semantic is not None
    assert rig.semantic.calls == []


def test_the_ask_names_the_window_and_the_trace_never_its_title() -> None:
    """SW-06/25: the person sees app, reference and the page title; the trace only tokens."""
    win = Windows()
    win.live[0x30]["title"] = "Zanzibar Quokka - YouTube"
    win.foreground = 0x30
    rig, _ = _rig(win)
    session = rig.session
    assert session is not None
    trace = ListTrace()
    session._trace = trace
    result = session.run_task(_plan(replace(_send("s3"), window="opened:o1")))
    assert [ask.window for ask in rig.confirmer.asked] == [
        "chrome (opened:o1): Zanzibar Quokka - YouTube"
    ]
    carried = "\n".join(
        [*trace.lines, repr(result), json.dumps([r.to_dict() for r in result.results])]
    )
    for sentinel in ("Zanzibar", "Quokka"):
        assert sentinel not in carried, sentinel


def test_generated_text_sent_in_another_window_is_shown_with_that_window() -> None:
    """SW-08: page-derived text sent in another window; the ask shows the text and where."""
    win = Windows()
    win.live[0x30]["title"] = "Inbox - Chrome"
    win.foreground = 0x30
    rig, _ = _rig(win)
    session = rig.session
    assert session is not None
    step = replace(_send("s3"), text_provenance="generated", window="opened:o1")
    session.run_step(step)
    assert len(rig.confirmer.asked) == 1
    ask = rig.confirmer.asked[0]
    assert ask.params.get("text") == HELLO
    assert ask.window == "chrome (opened:o1): Inbox - Chrome"


@pytest.mark.parametrize(("chars", "shown"), [(0, "(untitled)"), (80, "x" * 80), (81, "x" * 80)])
def test_the_window_label_title_boundaries(chars: int, shown: str) -> None:
    """SW-06 boundaries: empty, the cap, the cap plus one."""
    win = Windows()
    win.live[0x30]["title"] = "x" * chars
    rig, _ = _rig(win)
    session = rig.session
    assert session is not None
    session.run_step(_step("c1", window="opened:o1"))
    assert session.window_label() == f"chrome (opened:o1): {shown}"


def test_the_window_label_is_one_line() -> None:
    """SW-06: a page title cannot break the ask into lines of its own."""
    win = Windows()
    win.live[0x30]["title"] = "drake\nwindow: start\r\tYouTube"
    rig, _ = _rig(win)
    session = rig.session
    assert session is not None
    session.run_step(_step("c1", window="opened:o1"))
    assert session.window_label() == "chrome (opened:o1): drake window: start YouTube"


def test_a_step_back_in_its_first_window_is_done_already_there_only() -> None:
    """SW-37 / OW-04: a send done in one window is fresh in another and done on return."""
    rig, win = _rig()
    step = replace(_send("s3"), window="opened:o1")
    rig.ledger.set_window_ref("opened:o1")
    rig.ledger.executions[rig.ledger.key_for(step)] = {
        "step_id": "s3",
        "phase": "verified",
        "outcome": "delivered_verified",
    }
    rig.ledger.set_window_ref(None)
    session = rig.session
    assert session is not None
    win.foreground = 0x10
    other = session.run_step(replace(step, window="start"))
    assert other.gate == "confirm", "fresh in the start window: the person is asked"
    win.foreground = 0x30
    back = session.run_step(step)
    assert back.gate == "done_already"


def test_a_resumed_link_in_an_opened_window_needs_it_in_front_for_r2() -> None:
    """Tester finding (SW-15 on a resume): the link's window is one the task opened and the
    runner tapped the start window; the first run would stop `not_foreground`, so must this."""
    rig, win = _rig()
    win.foreground = 0x10
    rig.ledger.set_window_ref("opened:o1")  # the ledger restores the link's ref
    session = rig.session
    assert session is not None
    result = session.run_task(_plan(_send("p2.s3")), continuation=True)
    assert [(r.code, r.evidence.get("detail")) for r in result.results] == [
        (ErrorCode.FOCUS_LOST, "not_foreground")
    ]
    assert rig.confirmer.asked == []


# ---- bring a switched-to window forward (issue #38, packet switch-forward, gate REVISE 6) -------


def _tapping(rig: Rig, win: Windows, *, refuse: bool = False) -> list[int]:
    taps: list[int] = []

    def bring_forward(hwnd: int) -> str:
        taps.append(hwnd)
        if refuse:
            raise OSError("foreground refused")
        win.foreground = hwnd
        return "alt-tap"

    session = rig.session
    assert session is not None
    session._bring_forward = bring_forward
    return taps


def test_a_window_change_brings_the_window_forward_once() -> None:
    rig, win = _rig()
    taps = _tapping(rig, win)
    session = rig.session
    assert session is not None
    result = session.run_task(_plan(_step("c1", window="opened:o1", path="physical"), _step("c2")))
    assert taps == [0x30]
    assert result.results[0].evidence["foreground_after_select"] == "alt-tap"
    assert "foreground_after_select" not in result.results[1].evidence


def test_naming_the_current_window_taps_nothing() -> None:
    rig, win = _rig()
    taps = _tapping(rig, win)
    session = rig.session
    assert session is not None
    session.run_step(_step("c1", window="start"))
    assert taps == []


def test_an_open_taps_nothing() -> None:
    rig, win = _rig()
    taps = _tapping(rig, win)
    session = rig.session
    assert session is not None
    session.run_step(replace(_open(step_id="o2"), subgoal="g1"))
    assert taps == []


def test_the_tap_follows_the_boundary_and_a_lock_after_it_stops_before_observe() -> None:
    """SW-07: identity, boundary, tap, lock re-check, observe."""
    rig, win = _rig()
    order: list[str] = []
    taps = _tapping(rig, win)
    session = rig.session
    assert session is not None
    boundary = session._boundary

    def spy_boundary(evidence: dict[str, Any]) -> bool:
        order.append(f"boundary after {len(taps)} taps")
        return boundary(evidence)

    session._boundary = spy_boundary  # type: ignore[method-assign]
    session._locked = lambda: bool(taps)  # the session locks right after the tap
    observed = len(rig.world.observed)
    result = session.run_step(_step("c1", window="opened:o1", path="physical"))
    assert order[0] == "boundary after 0 taps", "the boundary guards the tap"
    assert taps, "and then the tap"
    assert (result.code, result.evidence["detail"]) == (ErrorCode.SECURE_DESKTOP, "session_locked")
    assert len(rig.world.observed) == observed, "nothing observed after the lock"


def test_a_refused_tap_is_evidence_and_the_step_still_runs() -> None:
    """Gate Q2: the executor's own checks are the postcondition."""
    rig, win = _rig()
    _tapping(rig, win, refuse=True)
    session = rig.session
    assert session is not None
    result = session.run_step(_step("c1", window="opened:o1", path="physical"))
    assert result.evidence["foreground_after_select"] is False
    assert result.action is not None, "the physical click still ran"


def test_an_r2_step_after_a_refused_tap_stops_not_foreground() -> None:
    rig, win = _rig()
    win.foreground = 0x10
    _tapping(rig, win, refuse=True)
    session = rig.session
    assert session is not None
    result = session.run_step(_step("c1", window="opened:o1", risk="R2"))
    assert (result.code, result.evidence["detail"]) == (ErrorCode.FOCUS_LOST, "not_foreground")


def test_a_tap_that_reports_success_but_leaves_the_window_behind_is_false() -> None:
    """Gate item 3: the evidence is the foreground re-read, never the procedure's word."""
    rig, win = _rig()
    session = rig.session
    assert session is not None
    session._bring_forward = lambda _hwnd: "alt-tap"  # says done, foreground unchanged
    win.foreground = 0x10
    result = session.run_step(_step("c1", window="opened:o1", path="physical"))
    assert result.evidence["foreground_after_select"] is False


# ---- switch-forward rulings (principal APPROVE-WITH-CHANGES on SF-01..43) ----------------------


def test_a_semantic_r0_step_after_a_switch_taps_nothing() -> None:
    """SF-13: a semantic action needs no foreground; every tap takes the person's focus."""
    rig, win = _rig()
    taps = _tapping(rig, win)
    session = rig.session
    assert session is not None
    session.run_step(_step("c1", window="opened:o1"))
    assert taps == []


def test_an_r2_step_after_a_switch_taps() -> None:
    rig, win = _rig()
    taps = _tapping(rig, win)
    session = rig.session
    assert session is not None
    session.run_step(_step("c1", window="opened:o1", risk="R2"))
    assert taps == [0x30]


def test_an_unread_foreground_is_never_a_success() -> None:
    """Gate blocking item 1: no foreground reader means no evidence of a tap that worked."""
    rig, win = _rig()
    _tapping(rig, win)
    session = rig.session
    assert session is not None
    session._foreground = None
    result = session.run_step(_step("c1", window="opened:o1", path="physical"))
    assert result.evidence["foreground_after_select"] is False


def test_a_guard_token_skips_the_tap_and_the_step_goes_on() -> None:
    """SF-01/05/06: held keys, accessibility filters, an elevated window in front."""
    for token in ("hands_on", "accessibility_filter", "foreground_elevated"):
        rig, win = _rig()
        taps = _tapping(rig, win)
        session = rig.session
        assert session is not None
        session._tap_guard = lambda token=token: token
        result = session.run_step(_step("c1", window="opened:o1", path="physical"))
        assert taps == [], token
        assert result.evidence["foreground_after_select"] is False
        assert result.evidence["tap_skipped"] == token


def test_a_secure_desktop_at_the_tap_stops() -> None:
    """SF-20."""
    rig, win = _rig()
    taps = _tapping(rig, win)
    session = rig.session
    assert session is not None
    session._tap_guard = lambda: "secure_desktop"
    result = session.run_step(_step("c1", window="opened:o1", path="physical"))
    assert (result.code, result.evidence["detail"]) == (ErrorCode.SECURE_DESKTOP, "secure_desktop")
    assert taps == []


def test_a_cloaked_window_is_on_another_desktop() -> None:
    """SF-10: DWMWA_CLOAKED."""
    win = Windows()
    win.live[0x30]["cloaked"] = True
    rig, _ = _rig(win)
    result = _stopped_with(rig, _step("c1", window="opened:o1"))
    assert (result.code, result.evidence["detail"]) == (
        ErrorCode.WINDOW_OTHER_DESKTOP,
        "window_other_desktop",
    )


def test_the_identity_is_read_again_right_before_the_tap() -> None:
    """SF-09: the window was minimized while the boundary waited."""
    rig, win = _rig()
    taps = _tapping(rig, win)
    session = rig.session
    assert session is not None
    boundary = session._boundary

    def minimized_during_the_wait(evidence: dict[str, Any]) -> bool:
        win.live[0x30]["minimized"] = True
        return boundary(evidence)

    session._boundary = minimized_during_the_wait  # type: ignore[method-assign]
    result = session.run_step(_step("c1", window="opened:o1", path="physical"))
    assert result.evidence["detail"] == "minimized"
    assert taps == []


def test_a_kill_right_before_the_tap_injects_nothing() -> None:
    """SF-32."""
    from tests.orchestrator.test_takeover_loop import FakeMonitor  # noqa: PLC0415

    rig, win = _rig()
    taps = _tapping(rig, win)
    session = rig.session
    assert session is not None
    monitor = FakeMonitor(rig.clock, last_user=-100.0)
    session._monitor = monitor
    boundary = session._boundary

    def killed_after_the_boundary(evidence: dict[str, Any]) -> bool:
        waited = boundary(evidence)
        monitor.kill = True
        return waited

    session._boundary = killed_after_the_boundary  # type: ignore[method-assign]
    result = session.run_step(_step("c1", window="opened:o1", path="physical"))
    assert result.evidence["detail"] == "killed"
    assert taps == []


def test_a_resumed_link_that_rebinds_another_window_taps_on_its_first_step() -> None:
    """SF-31: the runner tapped the start window; the rebind moved the task elsewhere."""
    rig, win = _rig()
    taps = _tapping(rig, win)
    rig.ledger.set_window_ref("opened:o1")
    session = rig.session
    assert session is not None
    session.run_task(_plan(_step("p2.c1", path="physical")), continuation=True)
    assert taps == [0x30]


def test_an_unreadable_tap_guard_skips_the_tap_and_never_kills_the_task() -> None:
    """Principal review of PR #46, item 2: the guard's readers raise OSError (integrity,
    input desktop, accessibility filters); a refused tap is evidence (gate Q2)."""
    rig, win = _rig()
    taps = _tapping(rig, win)
    session = rig.session
    assert session is not None

    def unreadable() -> str | None:
        raise OSError("cannot read this process's own integrity level")

    session._tap_guard = unreadable
    result = session.run_step(_step("c1", window="opened:o1", path="physical"))
    assert taps == []
    assert result.evidence["foreground_after_select"] is False
    assert result.evidence["tap_skipped"] == "guard_unreadable"


# ---- tester gaps on the switch tap (surviving mutants, PR #46) ---------------------------------


def test_a_physical_step_naming_the_current_window_taps_nothing() -> None:
    """Gate Q2: no change, no tap, for a step that needs the window in front too."""
    rig, win = _rig()
    taps = _tapping(rig, win)
    session = rig.session
    assert session is not None
    result = session.run_step(_step("c1", window="start", path="physical"))
    assert taps == []
    assert "foreground_after_select" not in result.evidence


def test_a_second_physical_step_in_the_switched_to_window_taps_nothing() -> None:
    """Addendum item 2: the tap follows a window change, never `_switched` alone."""
    rig, win = _rig()
    taps = _tapping(rig, win)
    session = rig.session
    assert session is not None
    plan = _plan(
        _step("c1", window="opened:o1", path="physical"),
        _step("c2", path="physical"),
        _step("c3", window="opened:o1", path="physical"),
    )
    result = session.run_task(plan)
    assert len(result.results) == 3
    assert taps == [0x30]


def test_a_physical_step_after_an_open_taps_nothing() -> None:
    """The launch already put the window in front (packet proposal 3)."""
    rig, win = _rig()
    taps = _tapping(rig, win)
    session = rig.session
    assert session is not None
    result = session.run_task(_plan(replace(_open(step_id="o2"), subgoal="g1"), _step("c1")))
    assert [r.evidence.get("window") for r in result.results][:1] == ["opened"]
    session.run_step(_step("c2", path="physical"))
    assert taps == []


def test_a_type_step_after_a_switch_taps_without_a_declared_path() -> None:
    """SF-13: `type` runs on the physical path by its verb, not only by `path`."""
    rig, win = _rig()
    taps = _tapping(rig, win)
    session = rig.session
    assert session is not None
    session.run_step(replace(_type("c1"), window="opened:o1"))
    assert taps == [0x30]


def test_an_r3_step_after_a_switch_taps() -> None:
    rig, win = _rig()
    taps = _tapping(rig, win)
    session = rig.session
    assert session is not None
    session.run_step(_step("c1", window="opened:o1", risk="R3"))
    assert taps == [0x30]


def test_a_step_raised_to_r2_by_its_name_taps() -> None:
    """SF-13: the effective tier decides (a deny pattern on the name), never the declared one."""
    rig, win = _rig()
    taps = _tapping(rig, win)
    session = rig.session
    assert session is not None
    session.run_step(_step("c1", name="Post", window="opened:o1"))
    assert taps == [0x30]


def test_a_locked_session_stops_as_locked_before_the_window_is_checked() -> None:
    """A locked session stops `session_locked`, never `window_gone` (issue #31)."""
    win = Windows()
    del win.live[0x30]
    rig, _ = _rig(win)
    session = rig.session
    assert session is not None
    session._locked = lambda: True
    result = _stopped_with(rig, _step("c1", window="opened:o1", path="physical"))
    assert (result.code, result.evidence["detail"]) == (ErrorCode.SECURE_DESKTOP, "session_locked")


def test_a_gone_window_stops_without_waiting_for_the_person() -> None:
    """Addendum item 1: the identity comes before the boundary's idle wait."""
    from tests.orchestrator.test_takeover_loop import FakeMonitor  # noqa: PLC0415

    win = Windows()
    del win.live[0x30]
    rig, _ = _rig(win)
    session = rig.session
    assert session is not None
    session._monitor = FakeMonitor(rig.clock, input_until=5.0)
    result = _stopped_with(rig, _step("c1", window="opened:o1", path="physical"))
    assert result.evidence["detail"] == "window_gone"
    assert "waited_s" not in result.evidence
    assert rig.clock["t"] == 0.0


def test_a_cloaked_window_stops_before_a_minimized_one() -> None:
    """SF-10: a window on another desktop also reads minimized there; the reason is the desktop."""
    win = Windows()
    win.live[0x30]["cloaked"] = True
    win.live[0x30]["minimized"] = True
    rig, _ = _rig(win)
    result = _stopped_with(rig, _step("c1", window="opened:o1"))
    assert result.evidence["detail"] == "window_other_desktop"


def test_a_lock_that_comes_with_a_skipped_tap_stops_before_observe() -> None:
    """SF-19: the lock re-check is in a `finally`: a skipped or refused tap is checked too."""
    for refuse, guard in ((False, "hands_on"), (True, None)):
        rig, win = _rig()
        _tapping(rig, win, refuse=refuse)
        session = rig.session
        assert session is not None
        read: list[str] = []

        def tap_guard(guard: str | None = guard, read: list[str] = read) -> str | None:
            read.append("guard")
            return guard

        session._tap_guard = tap_guard
        session._locked = lambda read=read: bool(read)
        observed = len(rig.world.observed)
        result = session.run_step(_step("c1", window="opened:o1", path="physical"))
        assert (result.code, result.evidence["detail"]) == (
            ErrorCode.SECURE_DESKTOP,
            "session_locked",
        ), guard
        assert len(rig.world.observed) == observed, "nothing observed after the lock"


def test_a_resume_taps_once_then_the_window_is_in_front() -> None:
    """SF-31: the pending tap is spent on the first step that needs it, once."""
    rig, win = _rig()
    taps = _tapping(rig, win)
    rig.ledger.set_window_ref("opened:o1")
    session = rig.session
    assert session is not None
    plan = _plan(_step("p2.c1"), _step("p2.c2", path="physical"), _step("p2.c3", path="physical"))
    result = session.run_task(plan, continuation=True)
    assert len(result.results) == 3
    assert taps == [0x30], "the semantic step kept the tap pending; the first physical spent it"


def test_a_resume_in_the_window_already_bound_taps_nothing() -> None:
    """SF-31: only a rebind that changed the window leaves a tap pending."""
    rig, win = _rig()
    taps = _tapping(rig, win)
    rig.ledger.set_window_ref("opened:o1")
    session = rig.session
    assert session is not None
    session.hwnd = 0x30  # the runner bound the opened window and tapped it
    session.run_task(_plan(_step("p2.c1", path="physical")), continuation=True)
    assert taps == []


def test_a_switch_on_a_semantic_step_still_taps_before_the_next_physical_step() -> None:
    """Tester T-01: the switch is carried to the first step that needs the window in front."""
    rig, win = _rig()
    taps = _tapping(rig, win)
    session = rig.session
    assert session is not None
    result = session.run_task(_plan(_step("c1", window="opened:o1"), _step("c2", path="physical")))
    assert len(result.results) == 2
    assert taps == [0x30]


def test_a_skipped_tap_is_tried_again_on_the_next_step_that_needs_it() -> None:
    """Tester N6: `hands_on` passes; the window is still behind."""
    rig, win = _rig()
    taps = _tapping(rig, win)
    session = rig.session
    assert session is not None
    reasons = ["hands_on", None]
    session._tap_guard = lambda: reasons.pop(0) if reasons else None
    session.run_task(
        _plan(_step("c1", window="opened:o1", path="physical"), _step("c2", path="physical"))
    )
    assert taps == [0x30], "the second step tapped once the keys were released"
