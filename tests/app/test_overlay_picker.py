"""The overlay's window-picker filter as a pure function (issue #48): no Tk root, no window, no
input, so it runs in the unit gate (`test_overlay.py` builds a real Tk window, `windows`)."""

from __future__ import annotations

from steerai.app.overlay import pickable
from steerai.probes.windows import WindowInfo

CHROME = WindowInfo(hwnd=0x10, title="Amir Khan - Chat - Google Chrome", pid=500)


def test_the_picker_never_offers_a_window_whose_cloak_cannot_be_read() -> None:
    """Picking it would end in `cloak_unreadable`; the person never sees a dead end."""
    unclear = WindowInfo(hwnd=0x20, title="Inbox - Google Chrome", pid=500, cloak_unreadable=True)
    assert pickable([CHROME, unclear], set(), set()) == [CHROME]


def test_the_picker_hides_steers_own_and_minimized_windows() -> None:
    console = WindowInfo(hwnd=0x30, title="python -m steerai.app", pid=700)
    child = WindowInfo(hwnd=0x31, title="runner", pid=701)
    small = WindowInfo(hwnd=0x40, title="Untitled - Notepad", pid=600, minimized=True)
    assert pickable([CHROME, console, child, small], {console.hwnd}, {child.pid}) == [CHROME]
