"""What each low-level hook event means ([V2.60] hook helper, packet rev 2b §4): the pure half of
the takeover monitor that runs where the hooks run.

Moved out of `monitor.py` unchanged so the hook helper process can load it without loading any
input-sending code (S6; a test pins the import closure). It keeps O(1) counters, the last own
event per hook and one `last_user` instant: never a key code, a position beyond the dead-band
reference, or anything that grows with the input (K11, K15). The key code is compared with the
probe key and nothing else.

Rules (edge cases TK-01..49, probe `hook-latency-2026-11-04`):
- Only Steer's own `dwExtraInfo` tag (the low 32 bits; a probe's sequence number rides above it,
  [V2.80]) exempts an event. Any other keyboard, button or wheel event is the person's,
  injected bit or not; an injected bare mouse move is counted but is never a takeover.
- Real mouse moves pass a dead band around the last cursor position any move set, so a nudged
  desk is not a takeover; wheel and button events always count.
- Input while an ask is open is the person's answer, not a takeover.
- Input carrying v1's constant tag in a run with its own tag is another Steer ([V2.58] (2)).
"""

from __future__ import annotations

from typing import Final

LLKHF_INJECTED: Final = 0x10
LLKHF_UP: Final = 0x80
"""KBDLLHOOKSTRUCT flags: the transition state, set for a key-up (Microsoft Learn)."""
LLMHF_INJECTED: Final = 0x01
WM_MOUSEMOVE: Final = 0x0200
WM_LBUTTONDOWN: Final = 0x0201
WM_MOUSEWHEEL: Final = 0x020A
DEAD_BAND_PX: Final = 4
"""Physical pixels; unmeasured across DPI settings (TK-42)."""
V1_TAG: Final = 0x5354_4545
"""v1's constant input tag (`win_input.STEER_EXTRA_INFO`, pinned equal by a test): kept here so
the helper need not import the input side to recognise it."""

TAKEOVER: Final = "takeover"
OTHER_STEER_INPUT: Final = "other_steer_input"
"""[V2.58] (2): the latest foreign input carried v1's constant tag in a run with its own tag:
another Steer, not a person at the keys."""
TAKEOVER_TOKENS: Final = frozenset({TAKEOVER, OTHER_STEER_INPUT})
"""The closed tokens of a stop for someone else's input: a person, or another Steer."""

TAG_MASK: Final = 0xFFFF_FFFF
PROBE_SEQ_SHIFT: Final = 32
PROBE_SEQ_MAX: Final = 0x7FFF_FFFF


def probe_tag(run_tag: int, seq: int) -> int:
    """[V2.80] #492: the `dwExtraInfo` of one probe event: the run's tag in the low 32 bits and
    the probe's sequence number (1..PROBE_SEQ_MAX) above it. An action event carries the bare
    run tag, so only a probe has a sequence. Needs a 64-bit `ULONG_PTR`."""
    if not 0 < run_tag <= TAG_MASK:
        raise ValueError("a run tag is a nonzero 32-bit value")
    if not 0 < seq <= PROBE_SEQ_MAX:
        raise ValueError("a probe sequence is 1..PROBE_SEQ_MAX")
    return run_tag | seq << PROBE_SEQ_SHIFT


def next_probe_seq(seq: int) -> int:
    """The sequence after `seq`, wrapping past PROBE_SEQ_MAX to 1 (0 means no probe). The wrap
    is unreachable: a ledger starts at a random number and 2**31 probes at the 1 s period take
    over 68 years."""
    return seq % PROBE_SEQ_MAX + 1


HOOKS: Final = ("keyboard", "mouse")
UNASSIGNED_VKS: Final = frozenset({0x0E, 0x0F, *range(0x97, 0xA0), 0xE8})
"""Microsoft Learn, Virtual-Key Codes (Winuser.h): 0x0E-0F, 0x97-9F and 0xE8 are Unassigned; the
keyboard probe is one of them."""


class Classifier:
    """The hook events' meaning: fed by the hook callbacks, read by the probe ledger and the
    loop. `extra_info` is the tag of this process's own input (v1's constant, or a v2 run's own,
    [V2.57]); `probe_vk` is the keyboard probe's key."""

    def __init__(
        self,
        *,
        started_at: float,
        extra_info: int,
        probe_vk: int,
        dead_band_px: int = DEAD_BAND_PX,
        numbered_probes: bool = False,
    ) -> None:
        self._extra_info = extra_info
        self._numbered = numbered_probes
        """[V2.80] #492: v2 (the hook monitor with `per_hook`, the helper): a probe-shaped event
        may carry a number. v1 keeps its exact compare, unchanged."""
        self._probe_vk = probe_vk
        self._dead_band = dead_band_px
        self._probe_seq = dict.fromkeys(HOOKS, 0)
        """[V2.80] #492: per hook, the sequence of the last probe event it saw (0: none). Only a
        probe event sets it (the keyboard's is the probe key's key-up, the mouse's an absolute
        move); no other own event is proof. A number, never a key code."""
        self._last_by = dict.fromkeys(HOOKS, started_at)
        """Each hook's last own event (LW-01)."""
        self._last_steer = started_at
        self._last_user: float | None = None
        self._last_kind = TAKEOVER
        self._reference: tuple[int, int] | None = None
        self._counts: dict[str, int] = {}
        self._ask_depth = 0
        self._kill = False
        self._errors = 0
        self._loop_failed = False

    # ---- what the hook thread feeds -------------------------------------------------------------

    def on_keyboard(self, *, flags: int, extra: int, now: float, vk: int = 0) -> None:
        probe = vk == self._probe_vk and bool(flags & LLKHF_UP)
        seq = self._own_seq(extra, probe_shaped=probe, injected=bool(flags & LLKHF_INJECTED))
        if seq is not None:
            self._steer(now, "keyboard", seq=seq)
            return
        if extra == V1_TAG:
            self._other_steer(now)
            return
        self._person(now, injected=bool(flags & LLKHF_INJECTED))

    def on_mouse(
        self, *, message: int, flags: int, extra: int, pt: tuple[int, int], now: float
    ) -> None:
        x, y = pt
        seq = self._own_seq(
            extra, probe_shaped=message == WM_MOUSEMOVE, injected=bool(flags & LLMHF_INJECTED)
        )
        if seq is not None:
            self._steer(now, "mouse", seq=seq)
            if message == WM_MOUSEMOVE:
                self._reference = (x, y)
            return
        if extra == V1_TAG:
            self._other_steer(now)
            return
        injected = bool(flags & LLMHF_INJECTED)
        if message != WM_MOUSEMOVE:
            self._person(now, injected=injected)
            return
        moved = self._reference is None or (
            abs(x - self._reference[0]) > self._dead_band
            or abs(y - self._reference[1]) > self._dead_band
        )
        if injected:
            self._reference = (x, y)
            self._count("injected_other")
            return
        if moved:
            # the reference moves only with a counted move, so a slow drift still adds up
            self._reference = (x, y)
            self._person(now, injected=False)
        else:
            self._count("user")

    def on_hotkey(self) -> None:
        self._kill = True

    def on_callback_error(self) -> None:
        self._errors += 1
        self._count("callback_errors")

    def on_loop_error(self) -> None:
        self._loop_failed = True

    def mark_ask_open(self) -> None:
        self._ask_depth += 1

    def mark_ask_closed(self) -> None:
        self._ask_depth = max(0, self._ask_depth - 1)

    def sync_ask_depth(self, depth: int) -> None:
        """The hook helper: Steer's `ask_depth` word, read directly before each event (K7)."""
        self._ask_depth = max(0, depth)

    # ---- what the ledger and the loop read ------------------------------------------------------

    def probe_seq(self, hook: str) -> int:
        return self._probe_seq[hook]

    def last_own(self, hook: str) -> float:
        return self._last_by[hook]

    def last_steer(self) -> float:
        return self._last_steer

    def last_user(self) -> float | None:
        """The last instant the person (or another Steer) acted; None until the first."""
        return self._last_user

    def last_kind(self) -> str:
        return self._last_kind

    def counts(self) -> dict[str, int]:
        return dict(self._counts)

    def kill_requested(self) -> bool:
        return self._kill

    def errors(self) -> int:
        return self._errors

    def loop_failed(self) -> bool:
        return self._loop_failed

    # ---- internals -----------------------------------------------------------------------------

    def _own_seq(self, extra: int, *, probe_shaped: bool, injected: bool) -> int | None:
        """[V2.80] #492: None when the event is not Steer's own; else its probe number (0: an
        action's event, the bare tag). Own is the exact tag, or (v2) a probe-shaped, injected
        event with the tag below and a number in range above. Any other numbered event is
        foreign: a person's."""
        if extra == self._extra_info:
            return 0
        if self._numbered and probe_shaped and injected and extra & TAG_MASK == self._extra_info:
            seq = extra >> PROBE_SEQ_SHIFT
            if 1 <= seq <= PROBE_SEQ_MAX:
                return seq
        return None

    def _steer(self, now: float, hook: str, *, seq: int) -> None:
        self._last_steer = now
        self._last_by[hook] = now
        if seq:
            self._probe_seq[hook] = seq
        self._count("steer")

    def _person(self, now: float, *, injected: bool) -> None:
        if self._ask_depth > 0:
            self._count("answer")
            return
        self._count("injected_other" if injected else "user")
        self._last_user = now
        self._last_kind = TAKEOVER

    def _other_steer(self, now: float) -> None:
        """[V2.58] (2): another Steer's input (v1's constant, in a run with its own tag). It
        stops this one as a person's would, counted apart."""
        self._count("other_steer")
        self._last_user = now
        self._last_kind = OTHER_STEER_INPUT

    def _count(self, kind: str) -> None:
        self._counts[kind] = self._counts.get(kind, 0) + 1
