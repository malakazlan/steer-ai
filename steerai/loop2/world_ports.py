"""The world ports (adapter slice 5; packet sections 2.1-2.4; [V2.41] wait; [V2.42] (A)).

The port semantics over two injected reads, so they run and are tested off Windows:
- `poll_read()`: the poll tier (the snapshot alone, [V2.10]);
- `full_from(seen)`: the full tier on a settled poll read (every fact).
The UIA wiring of those reads (`WorldModel`, `secure.apply`, `read_observed`) is slice 7.

`observe` hands over a process's window through `first_observation` on its first touch in the
task, and a plain full read after. `watch` holds by the verb's kind (`watch_kind`). `settle` is
`hold_still`, bounded by the load guard. Every hold sleeps through `kill_sleep`, so a kill ends
it at once. Observations are not stamped here: the runner passes every observation it uses
through `Ports.stamp` (RT-10/RT-11), so a stamp here would only be done twice.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, fields
from typing import Any, Final, Literal, Protocol, TypeVar

from steerai.errors import SteerError
from steerai.loop2.loop import Dispatch, PortInterrupted
from steerai.loop2.runner import LOAD_GUARD_S
from steerai.world.effects import Observed, ValueFact, top_document, value_fact
from steerai.world.elements import Element
from steerai.world.model import normalise_name
from steerai.world.observe2 import GROW_GUARD_S, FirstTouches, first_observation, hold_still
from steerai.world.watch import SUBMIT_GUARD_S, Watched, watch

KILL_POLL_S: Final = 0.05
"""[V2.40] Q4: a hold reads the switch at least this often (packet section 5)."""
WAIT_S: Final = GROW_GUARD_S
"""[V2.41]: the change-aware wait's deadline (3.0 s, provisional), not a sleep."""

WatchKind = Literal["plain", "trigger", "submit", "wait"]
T = TypeVar("T")
Wanted = Literal["on", "off", "selected", "expanded", "collapsed"]
WANTED: Final = frozenset({"on", "off", "selected", "expanded", "collapsed"})


class LiveElements(Protocol):
    """The live reads of one element, by its COM handle (slice 7: `UiaLiveReads` plus the
    identity read). A COM failure raises a `SteerError` with its code; a missing pattern reads
    None."""

    def identity(self, handle: Any) -> tuple[str, str, bool | None]:  # Any: a COM pointer
        """The element's live control-type role, name and IsPassword (None: not read)."""
        ...

    def value(self, handle: Any) -> str | None: ...  # Any: a COM element pointer
    def toggle_state(self, handle: Any) -> int | None: ...  # Any: a COM element pointer
    def is_selected(self, handle: Any) -> bool | None: ...  # Any: a COM element pointer
    def expand_state(self, handle: Any) -> int | None: ...  # Any: a COM element pointer


@dataclass(frozen=True)
class WorldTimes:
    """The watch and hold times, by class ([V2.48]). Live run 3 (2026-11-15, real sites)
    measured: first change p95 578 ms (max 1313), settle p95 2938 ms (max 4360), conclusive p95
    2203 ms (max 2843). Every value except `submit_guard_s` is unmeasured as a setting and
    provisional. Stage 1 measures, per class, the watches that end at the deadline and the
    seconds that adds per task."""

    poll_s: float = 0.1
    """Unmeasured / provisional: the poll tier's interval (the effect-pages probe's start)."""
    quiet_s: float = 0.5
    """Unmeasured / provisional: the quiet window of `watch`, `first_observation` and `settle`."""
    deadline_s: float = 2.0
    """Unmeasured / provisional: a non-trigger watch's guard ([V2.27] first change, 2 s); live
    run 3's first change p95 is 578 ms."""
    trigger_deadline_s: float = 2.0
    """Unmeasured / provisional: a trigger watch's guard ([V2.48], changed from 5.0). A trigger
    holds to it whenever its report is weak or none, so 5 s would cost every focus-only click
    5 s; a later navigation is caught by staleness ([V2.28]). Live run 3's first change of any
    kind came within 1.3 s."""
    submit_guard_s: float = SUBMIT_GUARD_S
    """[V2.42] (A): frozen at 5.0 s, a config value; live run 3's conclusive max is 2843 ms."""
    wait_s: float = WAIT_S
    """[V2.41], [V2.48]: the wait's deadline: it ends on a strong report followed by the quiet
    window, otherwise here. Unmeasured / provisional."""

    def __post_init__(self) -> None:
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, bool) or not (math.isfinite(value) and value > 0):
                raise ValueError(f"{f.name} must be finite and positive")


def kill_sleep(
    seconds: float,
    *,
    interrupted: Callable[[], str | None],
    pause: Callable[[float], None] = time.sleep,
) -> None:
    """[V2.40] Q4: sleep `seconds` in steps of at most `KILL_POLL_S`, reading the switch before
    every step and once at the end. Any token that is not None (an empty one too) raises
    `PortInterrupted`; nothing after it waits on."""
    left = seconds if math.isfinite(seconds) else 0.0
    while True:
        token = interrupted()
        if token is not None:
            raise PortInterrupted(token)
        if left <= 0:
            return
        step = min(left, KILL_POLL_S)
        pause(step)
        left -= step


def watch_kind(dispatch: Dispatch) -> WatchKind:
    """How the watch after a dispatch holds (§7.5, [V2.8], [V2.28] 1, [V2.41], [V2.48], [V2.50]).
    It reads only what the risk path decided and put on the dispatch, never the verb or the key
    again, so the timing and the risk classes cannot disagree:
    - `submit`: the risk path routed it through the Enter table (`Dispatch.submit`);
    - `wait`: a wait (it sends nothing), held like a trigger to `WAIT_S`;
    - `trigger`: the risk path classed it an activation with no read-back (`Dispatch.trigger`:
      a click on a non-field, an Enter or a Space that activates an acting element);
    - `plain`: everything else (the read-back verbs, focus, a press on a state-set element)."""
    if dispatch.submit:
        return "submit"
    if dispatch.action.verb == "wait":
        return "wait"
    return "trigger" if dispatch.trigger else "plain"


class WorldPorts:
    """`observe`, `watch` and `settle` for one task, and the last observation handed out (the
    one `act` resolves on, packet 2.1 and 2.5)."""

    def __init__(  # noqa: PLR0913 - the two reads, the switch, the clock and the task's state
        self,
        *,
        poll_read: Callable[[], Observed],
        full_from: Callable[[Observed], Observed],
        interrupted: Callable[[], str | None],
        clock: Callable[[], float],
        pause: Callable[[float], None] = time.sleep,
        touches: FirstTouches,
        times: WorldTimes,
        live: LiveElements,
        key: bytes,
        trace: Callable[[str], None],
    ) -> None:
        self._poll_read = poll_read
        self._full_from = full_from
        self._interrupted = interrupted
        self._clock = clock
        self._pause = pause
        self._touches = touches
        self._times = times
        self._live = live
        self._key = key
        self._trace = trace
        self.last: Observed | None = None
        """The last observation this adapter handed out, from observe, watch or settle."""

    def _sleep(self, seconds: float) -> None:
        kill_sleep(seconds, interrupted=self._interrupted, pause=self._pause)

    def _full(self) -> Observed:
        return self._full_from(self._poll_read())

    def _handed(self, seen: Observed) -> Observed:
        self.last = seen
        return seen

    def observe(self) -> Observed:
        """[V2.31]: through `first_observation`, which holds a process's first touch in this task
        and reads once for a process already handed over."""
        seen = first_observation(
            self._full,
            quiet_s=self._times.quiet_s,
            touches=self._touches,
            clock=self._clock,
            sleep=self._sleep,
        )
        return self._handed(seen)

    def watch(self, before: Observed, dispatch: Dispatch) -> Watched:
        """The effect of the dispatch just sent, held by its kind (`watch_kind`)."""
        kind = watch_kind(dispatch)
        times = self._times
        deadline = {
            "plain": times.deadline_s,
            "trigger": times.trigger_deadline_s,
            "submit": times.submit_guard_s,
            "wait": times.wait_s,
        }[kind]
        acted = dispatch.runtime_id or None
        top = top_document(before.snapshot) if kind == "wait" else None
        scope = None if top is None else top.runtime_id  # [V2.48]: a wait's frame noise is weak
        watched = watch(
            self._poll_read,
            before,
            acted=acted,
            text=dispatch.text if dispatch.action.verb == "type" else None,
            scope=scope,
            clock=self._clock,
            sleep=self._sleep,
            poll_s=times.poll_s,
            settle_s=times.quiet_s,
            deadline_s=deadline,
            full=self._full_from,
            trigger=kind in {"trigger", "wait"},
            submit=kind == "submit",
        )
        self._handed(watched.after)
        return watched

    def settle(self, observed: Observed, guard_s: float) -> Observed:
        """[V2.31] `hold_still` from `observed`, bounded by `LOAD_GUARD_S` ([V2.38]); a guard
        that is not a number, or below zero, holds for none."""
        guard = min(guard_s, LOAD_GUARD_S) if guard_s >= 0 else 0.0
        held = hold_still(
            self._full,
            observed,
            quiet_s=self._times.quiet_s,
            guard_s=guard,
            clock=self._clock,
            sleep=self._sleep,
            poll_s=self._times.poll_s,
        )
        return self._handed(held)

    # ---- the live reads (packet 2.7, 2.8) -----------------------------------------------------

    def fact_of(self, text: str) -> ValueFact:
        """The value fact a field holding exactly `text` reads as: the session key and the fold
        `read_value` uses, so a value claim compares digests, never text."""
        return value_fact(text, key=self._key)

    def read_value(self, element: Element) -> ValueFact | None:
        """[V2.28] item 3, [V2.29] Q2: the field's value fact, read live; None when unread."""
        handle = self._resolved(element)
        if handle is None:
            return None
        text = self._read(lambda: self._live.value(handle))
        return None if text is None else value_fact(text, key=self._key)

    def read_state(self, element: Element, wanted: str) -> bool | None:
        """[V2.29] Q2: whether the element is in the wanted state, read live; None when unread.
        A toggle that is neither on nor off (indeterminate), or an expand state that is neither
        expanded nor collapsed (partly, leaf), is not in either state."""
        if wanted not in WANTED:
            raise ValueError(f"wanted must be one of {sorted(WANTED)}")
        handle = self._resolved(element)
        if handle is None:
            return None
        if wanted in {"on", "off"}:
            toggle = self._read(lambda: self._live.toggle_state(handle))
            return None if toggle is None else toggle == (1 if wanted == "on" else 0)
        if wanted == "selected":
            return self._read(lambda: self._live.is_selected(handle))
        state = self._read(lambda: self._live.expand_state(handle))
        return None if state is None else state == (1 if wanted == "expanded" else 0)

    def read_secure(self, element: Element) -> bool | None:
        """AP-08: the field's IsPassword before keys go to it. True: a password (listed secure,
        or live); None: unread (its flag was not read at listing, it is not listed exactly once
        with a live handle, or the live read failed); False: read live and not a password."""
        last = self.last
        if last is None or not element.runtime_id:
            return None
        snapshot = last.snapshot
        matches = [i for i, e in enumerate(snapshot.elements) if e.runtime_id == element.runtime_id]
        if len(matches) != 1 or len(snapshot.handles) != len(snapshot.elements):
            return None
        found = snapshot.elements[matches[0]]
        if found.secure:
            return True
        if found.id in snapshot.unread_secure:
            return None
        handle = snapshot.handles[matches[0]]
        identity = self._read(lambda: self._live.identity(handle))
        return None if identity is None else identity[2]

    def _resolved(self, element: Element) -> Any:  # Any: a COM element pointer, or None
        """Resolve as `act` does (packet 2.7): the one element with this RuntimeId on the last
        observation handed out, with its live handle, never a secure or unread-secure field, and
        its live role and folded name the ones the runner listed. None: do not read."""
        handle = self._listed_handle(element)
        if handle is None:
            return None
        identity = self._read(lambda: self._live.identity(handle))
        if identity is None:
            return None
        role, name, secret = identity
        if secret is not False:  # a password now, or unread: never read it (fail closed)
            self._trace("read_secure_live")
            return None
        if (role, normalise_name(name)) != (element.role, normalise_name(element.name)):
            self._trace("read_identity_changed")
            return None
        return handle

    def _listed_handle(self, element: Element) -> Any:  # Any: a COM element pointer, or None
        """The live handle of the one element with this RuntimeId on the last observation;
        None when there is none, more than one, no live handles, or a secure or unread-secure
        field ([V2.26] (2): never read a field that may be a password)."""
        last = self.last
        if last is None or not element.runtime_id or element.secure:
            return None
        snapshot = last.snapshot
        matches = [i for i, e in enumerate(snapshot.elements) if e.runtime_id == element.runtime_id]
        if len(matches) != 1 or len(snapshot.handles) != len(snapshot.elements):
            return None
        found = snapshot.elements[matches[0]]
        if found.secure or found.id in snapshot.unread_secure:
            return None
        return snapshot.handles[matches[0]]

    def _read(self, read: Callable[[], T]) -> T | None:
        """One live read; a COM failure (a `SteerError` with its taxonomy code) is unread and
        traced by its code, never its message; any other error is not swallowed."""
        try:
            return read()
        except SteerError as exc:
            self._trace(f"read_unread:{exc.code.value}")
            return None
