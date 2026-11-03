"""Keyboard injection (design 5.9, [v1.0]): Unicode text and key chords through SendInput.

Text goes as KEYEVENTF_UNICODE events (layout independent, astral characters as surrogate pairs).
Chords use virtual keys with their scan codes and the extended flag where Windows expects it.
Dangerous combos are refused with `gate_blocked`, including every modifier superset, compared by
virtual key so synonyms cannot bypass the list (AX-03); the only override is a typed `GateApproval`
from a user confirmation (AX-02). Clipboard-paste chords are never injected raw (AX-05). A partial
injection releases the chord's modifiers and reports what was inserted (AX-07). Every event carries
Steer's dwExtraInfo tag.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from steerai.errors import ErrorCode, SteerError
from steerai.probes.win_input import (
    INPUT,
    build_keyboard_input,
    build_keyboard_unicode,
    scan_code_for,
    send_inputs,
)

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

MODIFIERS = {"ctrl": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B, "meta": 0x5B, "cmd": 0x5B}
NAMED_KEYS = {
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "esc": 0x1B,
    "escape": 0x1B,
    "space": 0x20,
    "backspace": 0x08,
    "delete": 0x2E,
    "del": 0x2E,
    "insert": 0x2D,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    **{f"f{n}": 0x70 + n - 1 for n in range(1, 13)},
}
EXTENDED_VKS = frozenset({0x2E, 0x2D, 0x24, 0x23, 0x21, 0x22, 0x26, 0x28, 0x25, 0x27, 0x5B})

BLOCKED_COMBOS = (
    "alt+f4",
    "win+l",
    "win+d",
    "alt+tab",
    "win+r",
    "win+x",
    "win+e",
    "ctrl+shift+esc",
)
ROUTED_COMBOS = ("ctrl+v", "shift+insert", "win+v")
"""Clipboard pastes go through the clipboard system action under policy, never raw injection."""


@dataclass(frozen=True, slots=True)
class GateApproval:
    """A user's explicit confirmation of an otherwise blocked chord (design 5.13); logged by the
    gate, never created from a plan or model output."""

    approved_by: Literal["user"]
    reason: str
    decision_id: str


class InjectionError(SteerError):
    """SendInput accepted fewer events than sent; delivery is uncertain."""

    def __init__(self, message: str, *, inserted: int, sent: int) -> None:
        super().__init__(ErrorCode.ELEMENT_NOT_ACTIONABLE, message)
        self.inserted = inserted
        self.sent = sent


def _parse(chord: str) -> tuple[frozenset[str], str]:
    parts = [p.strip().lower() for p in chord.split("+") if p.strip()]
    if not parts:
        raise SteerError(ErrorCode.INVALID_TEXT, "empty chord")
    mods = frozenset(parts[:-1])
    key = parts[-1]
    for mod in mods:
        if mod not in MODIFIERS:
            raise SteerError(ErrorCode.INVALID_TEXT, f"unknown modifier {mod!r} in {chord!r}")
    return frozenset("win" if m in ("meta", "cmd") else m for m in mods), key


def _key_vk(key: str, chord: str) -> int:
    if key in NAMED_KEYS:
        return NAMED_KEYS[key]
    if len(key) == 1 and key.isascii() and key.isalnum():
        return ord(key.upper())
    raise SteerError(ErrorCode.INVALID_TEXT, f"unknown key {key!r} in {chord!r}")


def chord_signature(chord: str) -> tuple[frozenset[str], int]:
    """(modifier set, virtual key): the identity used for blocking, immune to key synonyms."""
    mods, key = _parse(chord)
    return mods, _key_vk(key, chord)


def chord_to_vks(chord: str) -> list[int]:
    mods, vk = chord_signature(chord)
    order = [m for m in ("ctrl", "shift", "alt", "win") if m in mods]
    return [MODIFIERS[m] for m in order] + [vk]


def is_blocked_combo(chord: str) -> bool:
    """True for a blocked combo or any modifier superset of one (alt+shift+tab blocks alt+tab)."""
    mods, vk = chord_signature(chord)
    for blocked in BLOCKED_COMBOS:
        b_mods, b_vk = chord_signature(blocked)
        if vk == b_vk and b_mods <= mods:
            return True
    return False


def is_routed_combo(chord: str) -> bool:
    signature = chord_signature(chord)
    return any(chord_signature(routed) == signature for routed in ROUTED_COMBOS)


def text_to_inputs(text: str) -> list[INPUT]:
    """One key-down and key-up per UTF-16 code unit, so astral characters go as surrogate pairs."""
    if not text:
        raise SteerError(ErrorCode.INVALID_TEXT, "nothing to type")
    try:
        raw = text.encode("utf-16-le")
    except UnicodeEncodeError as exc:
        raise SteerError(ErrorCode.INVALID_TEXT, "text contains a lone surrogate") from exc
    units = [int.from_bytes(raw[i : i + 2], "little") for i in range(0, len(raw), 2)]
    records: list[INPUT] = []
    for unit in units:
        records.append(build_keyboard_unicode(unit, keyup=False))
        records.append(build_keyboard_unicode(unit, keyup=True))
    return records


def chord_to_inputs(chord: str) -> list[INPUT]:
    vks = chord_to_vks(chord)
    down = [
        build_keyboard_input(
            vk=vk, keyup=False, scan=scan_code_for(vk), extended=vk in EXTENDED_VKS
        )
        for vk in vks
    ]
    up = [
        build_keyboard_input(vk=vk, keyup=True, scan=scan_code_for(vk), extended=vk in EXTENDED_VKS)
        for vk in reversed(vks)
    ]
    return down + up


def type_text(text: str) -> None:
    send_inputs(text_to_inputs(text))


def press(chord: str, *, approval: GateApproval | None = None) -> None:
    """Inject a chord. Blocked combos need a user approval; routed combos are always refused."""
    if is_routed_combo(chord):
        raise SteerError(
            ErrorCode.GATE_BLOCKED, f"{chord!r} pastes the clipboard; use the clipboard action"
        )
    if is_blocked_combo(chord) and approval is None:
        raise SteerError(ErrorCode.GATE_BLOCKED, f"blocked shortcut {chord!r} needs user approval")
    records = chord_to_inputs(chord)
    try:
        send_inputs(records)
    except OSError as exc:
        inserted = _inserted_count(exc)
        modifiers = chord_to_vks(chord)[:-1]
        _release_modifiers(modifiers)
        raise InjectionError(
            f"chord {chord!r} partially injected ({inserted}/{len(records)})",
            inserted=inserted,
            sent=len(records),
        ) from exc


def _inserted_count(exc: OSError) -> int:
    # `_send` reports "SendInput sent k/n"; anything else counts as unknown (0).
    text = str(exc)
    marker = "sent "
    if marker in text and "/" in text:
        try:
            return int(text.split(marker, 1)[1].split("/", 1)[0])
        except ValueError:
            return 0
    return 0


def _release_modifiers(vks: list[int]) -> None:
    ups = [build_keyboard_input(vk=vk, keyup=True) for vk in reversed(vks)]
    try:
        send_inputs(ups)
    except OSError:
        return  # best effort: the user's next keystroke may still see a stuck modifier
