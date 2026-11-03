"""Keyboard injection (design 5.9): Unicode text and named key chords through SendInput.

Text goes as KEYEVENTF_UNICODE events (layout independent, astral characters as surrogate pairs).
Chords use virtual keys so shortcuts work. Dangerous combos are refused unless explicitly forced,
including every modifier superset (design 5.9 [v0.7]). Every event carries Steer's dwExtraInfo tag.
"""

from __future__ import annotations

from steerai.probes.win_input import (
    INPUT,
    build_keyboard_input,
    build_keyboard_unicode,
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


def _parse(chord: str) -> tuple[frozenset[str], str]:
    parts = [p.strip().lower() for p in chord.split("+") if p.strip()]
    if not parts:
        raise ValueError("empty chord")
    mods = frozenset(p for p in parts[:-1])
    key = parts[-1]
    for mod in mods:
        if mod not in MODIFIERS:
            raise ValueError(f"unknown modifier {mod!r} in {chord!r}")
    return frozenset("win" if m in ("meta", "cmd") else m for m in mods), key


def chord_to_vks(chord: str) -> list[int]:
    mods, key = _parse(chord)
    if key in NAMED_KEYS:
        vk = NAMED_KEYS[key]
    elif len(key) == 1 and key.isascii() and key.isalnum():
        vk = ord(key.upper())
    else:
        raise ValueError(f"unknown key {key!r} in {chord!r}")
    order = [m for m in ("ctrl", "shift", "alt", "win") if m in mods]
    return [MODIFIERS[m] for m in order] + [vk]


def is_blocked_combo(chord: str) -> bool:
    """True for a blocked combo or any modifier superset of one (alt+shift+tab blocks alt+tab)."""
    mods, key = _parse(chord)
    for blocked in BLOCKED_COMBOS:
        b_mods, b_key = _parse(blocked)
        if key == b_key and b_mods <= mods:
            return True
    return False


def text_to_inputs(text: str) -> list[INPUT]:
    """One key-down and key-up per UTF-16 code unit, so astral characters go as surrogate pairs."""
    raw = text.encode("utf-16-le")
    units = [int.from_bytes(raw[i : i + 2], "little") for i in range(0, len(raw), 2)]
    records: list[INPUT] = []
    for unit in units:
        records.append(build_keyboard_unicode(unit, keyup=False))
        records.append(build_keyboard_unicode(unit, keyup=True))
    return records


def chord_to_inputs(chord: str) -> list[INPUT]:
    vks = chord_to_vks(chord)
    down = [build_keyboard_input(vk=vk, keyup=False) for vk in vks]
    up = [build_keyboard_input(vk=vk, keyup=True) for vk in reversed(vks)]
    return down + up


def type_text(text: str) -> None:
    send_inputs(text_to_inputs(text))


def press(chord: str, *, force: bool = False) -> None:
    if is_blocked_combo(chord) and not force:
        raise ValueError(f"blocked shortcut {chord!r}; pass force=True to override")
    send_inputs(chord_to_inputs(chord))
