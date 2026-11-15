"""Adapter slice 5: the world ports (packet sections 2.1-2.4, [V2.41] wait, [V2.42] (A)).

The ports are the port semantics over injected reads: `observe` (the first touch of a process
held by `first_observation`), `watch` (the verb's kind picks the hold and the deadline), `settle`
(`hold_still`, bounded by the load guard), and `kill_sleep`, the one sleep every hold uses. The
UIA wiring is slice 7.
"""

from __future__ import annotations

import dataclasses
import itertools
import math
from collections.abc import Callable

import pytest

from steerai.errors import ErrorCode, SteerError
from steerai.loop2.loop import TRIGGER_CLASSES, Dispatch, PortInterrupted, action_class
from steerai.loop2.runner import LOAD_GUARD_S, run_task
from steerai.loop2.world_ports import (
    KILL_POLL_S,
    WAIT_S,
    WorldPorts,
    WorldTimes,
    kill_sleep,
    watch_kind,
)
from steerai.verify.predicates import FIELD_ROLES
from steerai.world.effects import Observed, poll_tier, value_fact
from steerai.world.elements import Element
from steerai.world.observe2 import GROW_GUARD_S, FirstTouches
from steerai.world.watch import SUBMIT_GUARD_S
from tests.action.fakes import UIA_E_ELEMENTNOTAVAILABLE, com_error
from tests.loop2.world_fakes import (
    FIELD,
    GO,
    MENU,
    PAGE,
    QUERY_PAGE,
    TAB,
    World,
    act,
    decision,
    element,
    observed,
    report,
    snapshot,
    task,
)


class Clock:
    """A fake monotonic clock that only `pause` moves."""

    def __init__(self) -> None:
        self.now = 1_000.0
        self.pauses: list[float] = []

    def __call__(self) -> float:
        return self.now

    def pause(self, seconds: float) -> None:
        self.pauses.append(seconds)
        self.now += seconds


# ---- kill_sleep --------------------------------------------------------------------------------


def test_kill_sleep_sleeps_in_steps_of_at_most_fifty_milliseconds() -> None:
    clock = Clock()
    reads: list[float] = []

    def interrupted() -> str | None:
        reads.append(clock.now)
        return None

    kill_sleep(0.23, interrupted=interrupted, pause=clock.pause)
    assert KILL_POLL_S == 0.05
    assert all(0 < p <= KILL_POLL_S for p in clock.pauses)
    assert math.isclose(sum(clock.pauses), 0.23)
    assert len(reads) >= len(clock.pauses)  # the switch is read before every step


def test_kill_sleep_ends_at_once_on_a_token() -> None:
    clock = Clock()
    answers = iter([None, None, "killed"])
    with pytest.raises(PortInterrupted) as err:
        kill_sleep(10.0, interrupted=lambda: next(answers), pause=clock.pause)
    assert err.value.token == "killed"  # noqa: S105 - an interrupt token, not a password
    assert len(clock.pauses) == 2


def test_an_empty_token_is_a_kill_too() -> None:
    clock = Clock()
    with pytest.raises(PortInterrupted):
        kill_sleep(1.0, interrupted=lambda: "", pause=clock.pause)
    assert clock.pauses == []


def test_a_zero_sleep_still_reads_the_switch() -> None:
    clock = Clock()
    with pytest.raises(PortInterrupted):
        kill_sleep(0.0, interrupted=lambda: "takeover", pause=clock.pause)


# ---- the times ---------------------------------------------------------------------------------


def test_the_default_times() -> None:
    times = WorldTimes()
    assert (times.poll_s, times.quiet_s) == (0.1, 0.5)
    assert (times.deadline_s, times.trigger_deadline_s) == (2.0, 2.0)  # [V2.48]: not 5.0
    assert times.submit_guard_s == SUBMIT_GUARD_S == 5.0
    assert times.wait_s == WAIT_S == GROW_GUARD_S == 3.0


@pytest.mark.parametrize("bad", [0.0, -1.0, math.nan, math.inf])
@pytest.mark.parametrize(
    "name", ["poll_s", "quiet_s", "deadline_s", "trigger_deadline_s", "submit_guard_s", "wait_s"]
)
def test_a_time_must_be_finite_and_positive(name: str, bad: float) -> None:
    with pytest.raises(ValueError, match=name):
        WorldTimes(**{name: bad})


# ---- the watch's kind --------------------------------------------------------------------------


def _dispatch(
    verb: str,
    target: Element | None = GO,
    *,
    submit: bool = False,
    key: str | None = None,
    param: str | None = None,
) -> Dispatch:
    action = act(verb, target=None if target is None else target.id, key=key, param=param)
    return Dispatch(
        action=action,
        runtime_id=() if target is None else target.runtime_id,
        role="" if target is None else target.role,
        name="" if target is None else target.name,
        risk="R0",
        provenance="goal_span" if verb == "type" else None,
        submit=submit,
        # [V2.50]: the trigger is what the risk path puts on the dispatch (`action_class`)
        trigger=target is not None and action_class(action, target.role) in TRIGGER_CLASSES,
        text="red shoes" if verb == "type" else None,
    )


def test_a_click_on_anything_but_a_field_is_a_trigger() -> None:
    assert watch_kind(_dispatch("click", GO)) == "trigger"
    assert watch_kind(_dispatch("click", MENU)) == "trigger"


@pytest.mark.parametrize("role", sorted(FIELD_ROLES))
def test_a_click_on_a_field_is_not_a_trigger(role: str) -> None:
    field = dataclasses.replace(FIELD, role=role)
    assert watch_kind(_dispatch("click", field)) == "plain"


def test_an_enter_on_an_acting_element_is_a_trigger_and_another_key_is_not() -> None:
    """[V2.50]: an Enter on a field is the Enter table's (a submit), on a Button a trigger."""
    assert watch_kind(_dispatch("press", GO, key="Enter")) == "trigger"
    assert watch_kind(_dispatch("press", GO, key="Tab")) == "plain"


@pytest.mark.parametrize("verb", ["type", "focus", "check", "uncheck", "select", "expand"])
def test_read_back_and_harmless_verbs_are_plain(verb: str) -> None:
    target = FIELD if verb in {"type", "focus"} else GO
    param = "text" if verb == "type" else None
    assert watch_kind(_dispatch(verb, target, param=param)) == "plain"


def test_a_submit_is_a_submit_whatever_its_verb() -> None:
    assert watch_kind(_dispatch("press", FIELD, key="Enter", submit=True)) == "submit"
    assert watch_kind(_dispatch("click", GO, submit=True)) == "submit"


def test_a_wait_is_a_wait() -> None:
    assert watch_kind(_dispatch("wait", None)) == "wait"


# ---- the ports over fake reads -----------------------------------------------------------------


class Screen:
    """The page the fake reads return; `full_from` adds the full tier's facts. `read_s` makes
    every poll read take that long ([V2.34]: times are read completions); `script`, when set,
    gives the page at each read's completion time."""

    def __init__(self, page: Observed, clock: Clock, read_s: float = 0.0) -> None:
        self.page = page
        self.clock = clock
        self.read_s = read_s
        self.polls = 0
        self.fulls = 0
        self.change_at: float | None = None
        self.changed: Observed | None = None
        self.script: Callable[[float], Observed] | None = None
        self.reads: list[tuple[float, Observed]] = []

    def _now(self) -> Observed:
        if self.script is not None:
            return self.script(self.clock.now)
        if self.change_at is not None and self.clock.now >= self.change_at:
            assert self.changed is not None
            return self.changed
        return self.page

    def poll_read(self) -> Observed:
        self.polls += 1
        self.clock.now += self.read_s
        seen = self._now()
        self.reads.append((self.clock.now, seen))
        return poll_tier(seen.snapshot)

    def full_from(self, seen: Observed) -> Observed:
        self.fulls += 1
        now = self._now()
        return dataclasses.replace(now, snapshot=seen.snapshot)


def _never() -> str | None:
    return None


def _ports(
    screen: Screen, clock: Clock, interrupted: Callable[[], str | None] = _never
) -> WorldPorts:
    return WorldPorts(
        poll_read=screen.poll_read,
        full_from=screen.full_from,
        interrupted=interrupted,
        clock=clock,
        pause=clock.pause,
        touches=FirstTouches(),
        times=WorldTimes(),
        live=Live(),
        key=KEY,
        trace=lambda _t: None,
    )


def test_observe_holds_the_first_touch_and_reads_once_after() -> None:
    clock = Clock()
    screen = Screen(observed(), clock)
    ports = _ports(screen, clock)
    first = ports.observe()
    held_reads = screen.polls
    assert held_reads > 1  # a page is held for one quiet window on its first touch
    second = ports.observe()
    assert screen.polls == held_reads + 1
    assert ports.last is second
    assert first.tier == "full"


def test_a_wait_with_no_change_returns_at_the_wait_deadline() -> None:
    clock = Clock()
    screen = Screen(observed(), clock)
    ports = _ports(screen, clock)
    before = ports.observe()
    start = clock.now
    watched = ports.watch(before, _dispatch("wait", None))
    assert watched.report.classes == frozenset({"none"})
    assert clock.now - start == pytest.approx(WAIT_S, abs=0.2)
    assert ports.last is watched.after


def test_a_wait_ends_early_once_a_change_has_settled() -> None:
    clock = Clock()
    screen = Screen(observed(), clock)
    ports = _ports(screen, clock)
    before = ports.observe()
    extra = element(77, "Text", "Results ready", (9, 77), 1)
    screen.changed = observed([*PAGE, extra])
    screen.change_at = clock.now + 0.3
    start = clock.now
    watched = ports.watch(before, _dispatch("wait", None))
    assert "none" not in watched.report.classes
    assert clock.now - start < WAIT_S


def test_a_kill_during_a_wait_ends_it_at_once() -> None:
    clock = Clock()
    screen = Screen(observed(), clock)
    reads = [0]

    def interrupted() -> str | None:
        reads[0] += 1
        return "killed" if reads[0] > 40 else None

    ports = _ports(screen, clock, interrupted)
    before = ports.observe()
    start = clock.now
    with pytest.raises(PortInterrupted):
        ports.watch(before, _dispatch("wait", None))
    assert clock.now - start < WAIT_S


def test_a_submit_holds_to_the_submit_guard() -> None:
    clock = Clock()
    screen = Screen(observed(), clock)
    ports = _ports(screen, clock)
    before = ports.observe()
    start = clock.now
    watched = ports.watch(before, _dispatch("press", FIELD, key="Enter", submit=True))
    assert watched.timed_out
    assert clock.now - start == pytest.approx(SUBMIT_GUARD_S, abs=0.2)


def test_a_plain_verb_ends_one_quiet_window_after_nothing_changed() -> None:
    clock = Clock()
    screen = Screen(observed(), clock)
    ports = _ports(screen, clock)
    before = ports.observe()
    start = clock.now
    ports.watch(before, _dispatch("check", GO))
    assert clock.now - start < WorldTimes().deadline_s


def test_settle_holds_until_quiet_and_is_remembered() -> None:
    clock = Clock()
    screen = Screen(observed(), clock)
    ports = _ports(screen, clock)
    first = ports.observe()
    start = clock.now
    settled = ports.settle(first, 4.0)
    assert ports.last is settled
    assert clock.now - start >= WorldTimes().quiet_s  # held for a quiet window on stable reads


@pytest.mark.parametrize(
    ("guard", "bound"), [(math.nan, 0.0), (-1.0, 0.0), (LOAD_GUARD_S * 3, LOAD_GUARD_S)]
)
def test_the_settle_guard_is_bounded_by_the_load_guard(guard: float, bound: float) -> None:
    clock = Clock()
    shaky = dataclasses.replace(observed(), snapshot=snapshot(PAGE, stable=False))
    screen = Screen(shaky, clock)  # never quiet: the hold runs to its guard
    ports = _ports(screen, clock)
    start = clock.now
    ports.settle(shaky, guard)
    assert clock.now - start <= bound + 2 * WorldTimes().poll_s


# ---- the live reads: read_value, read_state, fact_of (packet 2.7, 2.8) ------------------------

KEY = bytes(range(32))


class Live:
    """The live element reads, by handle: the identity (role, name), the value and the states.
    Every call is logged, so a test sees that a secure field was never read."""

    def __init__(self) -> None:
        self.ids: dict[object, tuple[str, str]] = {}
        self.values: dict[object, str | None] = {}
        self.toggles: dict[object, int | None] = {}
        self.selected: dict[object, bool | None] = {}
        self.expanded: dict[object, int | None] = {}
        self.fail: BaseException | None = None
        self.secure: dict[object, bool | None] = {}
        self.calls: list[tuple[str, object]] = []

    def _log(self, what: str, handle: object) -> None:
        self.calls.append((what, handle))
        if self.fail is not None:
            raise self.fail

    def identity(self, handle: object) -> tuple[str, str, bool | None]:
        self.calls.append(("identity", handle))
        role, name = self.ids[handle]
        return role, name, self.secure.get(handle, False)

    def value(self, handle: object) -> str | None:
        self._log("value", handle)
        return self.values.get(handle)

    def toggle_state(self, handle: object) -> int | None:
        self._log("toggle", handle)
        return self.toggles.get(handle)

    def is_selected(self, handle: object) -> bool | None:
        self._log("selected", handle)
        return self.selected.get(handle)

    def expand_state(self, handle: object) -> int | None:
        self._log("expand", handle)
        return self.expanded.get(handle)


BOX = element(42, "CheckBox", "Shop", (9, 42), 1)
SECRET = element(43, "Edit", "Password", (9, 43), 1, secure=True)


def _live_page(*extra: Element) -> Observed:
    page = observed([*PAGE, *extra])
    handles = [f"h{e.id}" for e in page.snapshot.elements]
    return dataclasses.replace(page, snapshot=dataclasses.replace(page.snapshot, handles=handles))


def _reader(live: Live, page: Observed, traced: list[str] | None = None) -> WorldPorts:
    clock = Clock()
    screen = Screen(page, clock)
    ports = WorldPorts(
        poll_read=screen.poll_read,
        full_from=screen.full_from,
        interrupted=lambda: None,
        clock=clock,
        pause=clock.pause,
        touches=FirstTouches(),
        times=WorldTimes(),
        live=live,
        key=KEY,
        trace=(traced if traced is not None else []).append,
    )
    ports.last = page
    return ports


def test_a_value_is_read_live_as_a_value_fact() -> None:
    live = Live()
    page = _live_page()
    live.ids["h2"] = ("Edit", "Search")
    live.values["h2"] = "red  shoes"
    ports = _reader(live, page)
    assert ports.read_value(FIELD) == value_fact("red shoes", key=KEY)
    assert ports.fact_of("red shoes") == value_fact("red shoes", key=KEY)


def test_an_element_whose_live_identity_changed_is_unread() -> None:
    live = Live()
    live.ids["h2"] = ("Edit", "Email")  # the field was re-rendered under another name
    live.values["h2"] = "red shoes"
    ports = _reader(live, _live_page())
    assert ports.read_value(FIELD) is None
    assert ("value", "h2") not in live.calls


def test_a_name_is_compared_folded() -> None:
    live = Live()
    live.ids["h2"] = ("Edit", "  Search ")
    live.values["h2"] = "x"
    assert _reader(live, _live_page()).read_value(FIELD) is not None


@pytest.mark.parametrize("unread", [False, True])
def test_a_secure_field_is_never_read(unread: bool) -> None:
    live = Live()
    target = dataclasses.replace(SECRET, secure=not unread)
    page = _live_page(target)
    if unread:
        unread_set = frozenset({target.id})
        page = dataclasses.replace(
            page, snapshot=dataclasses.replace(page.snapshot, unread_secure=unread_set)
        )
    live.ids[f"h{target.id}"] = ("Edit", "Password")
    live.values[f"h{target.id}"] = "hunter2"
    ports = _reader(live, page)
    assert ports.read_value(target) is None
    assert live.calls == []


def test_an_element_no_longer_listed_is_unread() -> None:
    live = Live()
    ports = _reader(live, _live_page())
    gone = element(99, "Edit", "Gone", (9, 99), 1)
    assert ports.read_value(gone) is None
    assert live.calls == []


def test_a_snapshot_without_live_handles_is_unread() -> None:
    live = Live()
    ports = _reader(live, observed())
    assert ports.read_value(FIELD) is None


def test_a_com_failure_is_unread_and_traced_by_its_code() -> None:
    live = Live()
    live.ids["h2"] = ("Edit", "Search")
    live.fail = SteerError(ErrorCode.PERCEPTION_TIMEOUT, "provider gave up on 'Search'")
    traced: list[str] = []
    assert _reader(live, _live_page(), traced).read_value(FIELD) is None
    assert traced == ["read_unread:perception_timeout"]


def test_a_programming_error_is_not_swallowed() -> None:
    live = Live()
    live.ids["h2"] = ("Edit", "Search")
    live.fail = KeyError("bug")
    with pytest.raises(KeyError):
        _reader(live, _live_page()).read_value(FIELD)


@pytest.mark.parametrize(
    ("wanted", "toggle", "expected"),
    [("on", 1, True), ("on", 0, False), ("off", 0, True), ("on", 2, False), ("on", None, None)],
)
def test_a_toggle_state_is_read_live(
    wanted: str, toggle: int | None, expected: bool | None
) -> None:
    live = Live()
    live.ids["h42"] = ("CheckBox", "Shop")
    live.toggles["h42"] = toggle
    assert _reader(live, _live_page(BOX)).read_state(BOX, wanted) is expected


@pytest.mark.parametrize(
    ("wanted", "expand", "expected"),
    [
        ("expanded", 1, True),
        ("collapsed", 0, True),
        ("expanded", 3, False),
        ("collapsed", None, None),
    ],
)
def test_an_expand_state_is_read_live(
    wanted: str, expand: int | None, expected: bool | None
) -> None:
    live = Live()
    live.ids["h42"] = ("CheckBox", "Shop")
    live.expanded["h42"] = expand
    assert _reader(live, _live_page(BOX)).read_state(BOX, wanted) is expected


def test_selected_is_read_live() -> None:
    live = Live()
    live.ids["h42"] = ("CheckBox", "Shop")
    live.selected["h42"] = True
    assert _reader(live, _live_page(BOX)).read_state(BOX, "selected") is True


def test_a_state_of_a_changed_element_is_unread() -> None:
    live = Live()
    live.ids["h42"] = ("Button", "Shop")
    live.toggles["h42"] = 1
    assert _reader(live, _live_page(BOX)).read_state(BOX, "on") is None


def test_an_unknown_wanted_state_is_a_programming_error() -> None:
    live = Live()
    live.ids["h42"] = ("CheckBox", "Shop")
    with pytest.raises(ValueError, match="wanted"):
        _reader(live, _live_page(BOX)).read_state(BOX, "maybe")


# ---- [V2.48]: timing by classification, the wait's ending, slow reads on every class ------------


def test_the_watch_kind_follows_the_risk_paths_enter_table_classification() -> None:
    """[V2.48] (1): the risk path routes a click that submits pending text through the Enter
    table (`Dispatch.submit`); `watch_kind` reads that same classification, so the two cannot
    disagree: the click gets submit timing, a plain click gets trigger timing."""
    typed = act("type", target=FIELD.id, param="text")
    world = World(
        [decision(typed, first=True), decision(act("click", target=GO.id))],
        effects=[report("own_value"), report("none")],
        screen=observed(QUERY_PAGE, focus=FIELD.runtime_id),
    )
    run_task(task(), world.ports())
    assert [(d.action.verb, d.submit, watch_kind(d)) for d in world.dispatched] == [
        ("type", False, "plain"),
        ("click", True, "submit"),
    ]
    plain = World([decision(act("click", target=GO.id), first=True)], effects=[report("own_state")])
    run_task(task(), plain.ports())
    assert [watch_kind(d) for d in plain.dispatched] == ["trigger"]


def _moved_focus() -> Observed:
    return observed(focus=FIELD.runtime_id)


def _renamed_tab() -> Observed:
    """Frame noise: the browser tab's name changes, outside the page's Document."""
    return observed(
        [e if e.id != TAB.id else dataclasses.replace(TAB, name="Shop (1)") for e in PAGE]
    )


@pytest.mark.parametrize("noise", [_moved_focus, _renamed_tab])
def test_a_wait_is_not_ended_by_a_focus_move_or_frame_noise(noise: Callable[[], Observed]) -> None:
    """[V2.48] (2): only a strong report followed by the quiet window ends a wait early."""
    clock = Clock()
    screen = Screen(observed(), clock)
    ports = _ports(screen, clock)
    before = ports.observe()
    screen.changed = noise()
    screen.change_at = clock.now + 0.3
    start = clock.now
    ports.watch(before, _dispatch("wait", None))
    assert clock.now - start == pytest.approx(WAIT_S, abs=0.2)


DEADLINES = {
    "plain": ("check", 2.0),
    "trigger": ("click", 2.0),
    "submit": ("submit", 5.0),
    "wait": ("wait", 3.0),
}


def _class_dispatch(kind: str) -> Dispatch:
    if kind == "plain":
        return _dispatch("check", GO)
    if kind == "trigger":
        return _dispatch("click", GO)
    if kind == "submit":
        return _dispatch("press", FIELD, key="Enter", submit=True)
    return _dispatch("wait", None)


def _churning(until: float) -> Callable[[float], Observed]:
    """A page that changes inside its Document on every read before `until`, then holds the
    last state it showed (it never reverts, which would be one more change)."""
    last: list[Observed] = []

    def page(now: float) -> Observed:
        if now >= until and last:
            return last[-1]
        extra = element(90, "Text", f"tick {int(now * 1000)}", (9, 90), 1)
        last.append(observed([*PAGE, extra]))
        return last[-1]

    return page


@pytest.mark.parametrize("kind", sorted(DEADLINES))
def test_no_watch_ends_on_a_read_that_saw_a_change(kind: str) -> None:
    """[V2.34], [V2.48] (3), every class: with slow reads (0.7 s, longer than the quiet window)
    and a page that changes up to just past the deadline, the watch never ends on the read
    that saw the last change: one more read follows it."""
    clock = Clock()
    screen = Screen(observed(), clock)
    ports = _ports(screen, clock)
    before = ports.observe()
    deadline = DEADLINES[kind][1]
    screen.read_s = 0.7
    screen.reads.clear()
    screen.script = _churning(clock.now + deadline + 0.6)
    ports.watch(before, _class_dispatch(kind))
    seen = [page for _, page in screen.reads]
    changed = [_changes(old, new) for old, new in itertools.pairwise(seen)]
    assert any(changed)  # the page did change during the watch
    assert not changed[-1]  # and the final read saw none: one more read followed the last change


@pytest.mark.parametrize("kind", sorted(DEADLINES))
def test_a_quiet_window_needs_two_reads_that_span_it(kind: str) -> None:
    """[V2.34], every class: one slow read is never a quiet window: a watch on a page that
    does not change still takes at least two reads before it ends."""
    clock = Clock()
    screen = Screen(observed(), clock)
    ports = _ports(screen, clock)
    before = ports.observe()
    screen.read_s = 0.7
    screen.reads.clear()
    start = clock.now
    ports.watch(before, _class_dispatch(kind))
    assert len(screen.reads) >= 2
    times = [at for at, _ in screen.reads]
    assert times[-1] - times[0] >= WorldTimes().quiet_s  # the reads span the window
    assert times[0] > start  # read times are completions, after the watch began


def _changes(old: Observed, new: Observed) -> bool:
    return [e.name for e in old.snapshot.elements] != [e.name for e in new.snapshot.elements]


@pytest.mark.parametrize("kind", sorted(DEADLINES))
def test_a_strong_change_is_followed_by_a_read_across_the_quiet_window(kind: str) -> None:
    """[V2.34], [V2.48] (2)/(3), every class: after a strong change (content inside the page),
    the watch never ends on the read that saw it: a later read spans the quiet window first."""
    clock = Clock()
    screen = Screen(observed(), clock)
    ports = _ports(screen, clock)
    before = ports.observe()
    screen.read_s = 0.7
    screen.reads.clear()
    extra = element(77, "Text", "Results ready", (9, 77), 1)
    screen.changed = observed([*PAGE, extra])
    screen.change_at = clock.now + 0.1  # the first read sees it
    ports.watch(before, _class_dispatch(kind))
    first_seen = next(at for at, page in screen.reads if _changes(observed(), page))
    after = [at for at, _ in screen.reads if at > first_seen]
    assert after
    assert after[-1] - first_seen >= WorldTimes().quiet_s


# ---- what the watch is told about the action ---------------------------------------------------


def test_the_watch_is_told_the_acted_element() -> None:
    """A check that only flips its own box is `own_state`, which needs the acted element: told
    nothing, the same change reads as `content_changed`."""
    clock = Clock()
    off = dataclasses.replace(BOX, caps=("toggle:off",))
    on = dataclasses.replace(BOX, caps=("toggle:on",))
    screen = Screen(observed([*PAGE, off]), clock)
    ports = _ports(screen, clock)
    before = ports.observe()
    screen.changed = observed([*PAGE, on])
    screen.change_at = clock.now + 0.1
    watched = ports.watch(before, _dispatch("check", off))
    assert "own_state" in watched.report.classes


def test_the_watch_is_told_the_typed_text() -> None:
    """The typed words showing up where they did not (here the window title) are a carrier,
    which needs the text: told nothing, there is no carrier."""
    clock = Clock()
    screen = Screen(observed(), clock)
    ports = _ports(screen, clock)
    before = ports.observe()
    titled = observed()
    screen.changed = dataclasses.replace(
        titled, snapshot=dataclasses.replace(titled.snapshot, title="red shoes - Shop")
    )
    screen.change_at = clock.now + 0.1
    watched = ports.watch(before, _dispatch("type", FIELD, param="text"))
    assert "title" in watched.report.carriers


# ---- #348 review: a kill ends every hold within one step ---------------------------------------


class LatchAt:
    """`interrupted()` that reports `killed` once the fake clock passes `at` (the kill key)."""

    def __init__(self, clock: Clock, at: float) -> None:
        self.clock = clock
        self.at = at

    def __call__(self) -> str | None:
        return "killed" if self.clock.now >= self.at else None


def _unstable() -> Observed:
    return dataclasses.replace(observed(), snapshot=snapshot(PAGE, stable=False))


def test_a_kill_during_the_first_touch_hold_ends_it_within_one_step() -> None:
    clock = Clock()
    screen = Screen(_unstable(), clock)  # never quiet: the first touch would hold to its deadline
    latch = LatchAt(clock, clock.now + 0.3)
    ports = _ports(screen, clock, latch)
    with pytest.raises(PortInterrupted):
        ports.observe()
    assert clock.now - latch.at <= KILL_POLL_S + 1e-9


def test_a_kill_during_settle_ends_it_within_one_step() -> None:
    clock = Clock()
    screen = Screen(_unstable(), clock)
    ports = _ports(screen, clock)
    first = screen.full_from(screen.poll_read())
    latch = LatchAt(clock, clock.now + 0.3)
    ports = _ports(screen, clock, latch)
    with pytest.raises(PortInterrupted):
        ports.settle(first, 4.0)
    assert clock.now - latch.at <= KILL_POLL_S + 1e-9


@pytest.mark.parametrize("kind", ["plain", "trigger", "submit"])
def test_a_kill_during_a_watch_ends_it_within_one_step(kind: str) -> None:
    clock = Clock()
    screen = Screen(observed(), clock)
    before = _ports(screen, clock).observe()
    latch = LatchAt(clock, clock.now + 0.3)
    ports = _ports(screen, clock, latch)
    with pytest.raises(PortInterrupted):
        ports.watch(before, _class_dispatch(kind))
    assert clock.now - latch.at <= KILL_POLL_S + 1e-9


# ---- #348 review: the live reads resolve by RuntimeId, never the snapshot id ------------------


def test_a_read_resolves_by_runtime_id_never_the_snapshot_id() -> None:
    """`last` holds an element with FIELD's snapshot id, role and name but another RuntimeId:
    it is not FIELD, so nothing is read."""
    live = Live()
    other = dataclasses.replace(FIELD, runtime_id=(9, 999))
    page = _live_page()
    swapped = [other if e.id == FIELD.id else e for e in page.snapshot.elements]
    page = dataclasses.replace(page, snapshot=dataclasses.replace(page.snapshot, elements=swapped))
    live.ids["h2"] = ("Edit", "Search")
    live.values["h2"] = "red shoes"
    ports = _reader(live, page)
    assert ports.read_value(FIELD) is None
    assert live.calls == []


@pytest.mark.parametrize("secure", [True, None])
def test_a_field_that_turned_secure_since_the_listing_is_not_read(secure: bool | None) -> None:
    """The live identity read also reads IsPassword: a field that is a password now, or whose
    flag cannot be read, is never read (fail closed), whatever the last observation said."""
    live = Live()
    live.ids["h2"] = ("Edit", "Search")
    live.values["h2"] = "hunter2"
    live.secure["h2"] = secure
    traced: list[str] = []
    assert _reader(live, _live_page(), traced).read_value(FIELD) is None
    assert ("value", "h2") not in live.calls
    assert traced == ["read_secure_live"]


# ---- AP-08: the IsPassword read before keys go to a field ---------------------------------------


@pytest.mark.parametrize(("live_flag", "expected"), [(False, False), (True, True), (None, None)])
def test_the_secure_flag_is_read_live(live_flag: bool | None, expected: bool | None) -> None:
    live = Live()
    live.ids["h2"] = ("Edit", "Search")
    live.secure["h2"] = live_flag
    assert _reader(live, _live_page()).read_secure(FIELD) is expected


def test_a_listed_secure_field_is_secure_without_a_read() -> None:
    live = Live()
    assert _reader(live, _live_page(SECRET)).read_secure(SECRET) is True
    assert live.calls == []


def test_a_listed_unread_secure_flag_is_unread_without_a_read() -> None:
    live = Live()
    page = _live_page()
    page = dataclasses.replace(
        page, snapshot=dataclasses.replace(page.snapshot, unread_secure=frozenset({FIELD.id}))
    )
    assert _reader(live, page).read_secure(FIELD) is None
    assert live.calls == []


def test_the_secure_flag_of_an_element_not_listed_once_is_unread() -> None:
    live = Live()
    gone = element(99, "Edit", "Gone", (9, 99), 1)
    assert _reader(live, _live_page()).read_secure(gone) is None
    twice = _live_page(dataclasses.replace(FIELD, id=98))
    assert _reader(live, twice).read_secure(FIELD) is None


def test_a_failed_secure_read_is_unread() -> None:
    class Failing(Live):
        def identity(self, _handle: object) -> tuple[str, str, bool | None]:
            raise SteerError(ErrorCode.PERCEPTION_TIMEOUT, "timeout")

    assert _reader(Failing(), _live_page()).read_secure(FIELD) is None


# ---- [V2.49]: a wait with no Document is scoped to the whole window ----------------------------


def test_a_wait_in_a_window_with_no_document_sees_the_whole_window() -> None:
    """A native window has no Document: the wait falls back to the whole window, so a change
    anywhere in it is a change (it ends early), as the scope rule says for native apps."""
    native = [e for e in PAGE if e.role != "Document"]
    clock = Clock()
    screen = Screen(observed(native), clock)
    ports = _ports(screen, clock)
    before = ports.observe()
    extra = element(77, "Text", "Saved", (9, 77), 0)
    screen.changed = observed([*native, extra])
    screen.change_at = clock.now + 0.3
    start = clock.now
    watched = ports.watch(before, _dispatch("wait", None))
    assert "none" not in watched.report.classes
    assert clock.now - start < WAIT_S


# ---- AP-27: a raw COMError never reaches the runner -------------------------------------------


def test_a_raw_com_error_in_a_live_read_is_unread_by_its_code() -> None:
    """A COMError the live read did not map itself goes through `execution.code_for`."""

    class Raw(Live):
        def value(self, handle: object) -> str | None:
            del handle
            raise com_error(UIA_E_ELEMENTNOTAVAILABLE)

    live = Raw()
    live.ids["h2"] = ("Edit", "Search")
    traced: list[str] = []
    assert _reader(live, _live_page(), traced).read_value(FIELD) is None
    assert traced == ["read_unread:element_not_found"]


def test_a_programming_error_in_a_live_read_is_not_swallowed() -> None:
    class Broken(Live):
        def value(self, handle: object) -> str | None:
            del handle
            raise KeyError("bug")

    live = Broken()
    live.ids["h2"] = ("Edit", "Search")
    with pytest.raises(KeyError):
        _reader(live, _live_page()).read_value(FIELD)
