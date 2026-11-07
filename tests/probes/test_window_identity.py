"""`window_identity` reports the fields a selection checks, `cloaked` included (issue #38,
hunter SF-10), against fake readers so the shape is checked without a desktop."""

from __future__ import annotations

import ctypes
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from steerai.probes import windows


@pytest.mark.parametrize("cloaked", [False, True, None])
def test_the_identity_carries_whether_dwm_cloaks_the_window(
    monkeypatch: pytest.MonkeyPatch, cloaked: bool | None
) -> None:
    user32 = SimpleNamespace(
        IsWindowVisible=lambda _h: 1,
        GetAncestor=lambda h, _flag: h,
        IsIconic=lambda _h: 0,
    )
    monkeypatch.setattr(windows, "is_window", lambda _h: True)
    monkeypatch.setattr(windows, "window_pid", lambda _h: 500)
    monkeypatch.setattr(windows, "process_start_time", lambda _p: 7)
    monkeypatch.setattr(windows, "process_name", lambda _p: "Chrome.exe")
    monkeypatch.setattr(windows, "window_title", lambda _h: "x")
    monkeypatch.setattr(windows, "_user32", lambda: user32)
    monkeypatch.setattr(windows, "is_cloaked", lambda _h: cloaked)
    identity = windows.window_identity(0x30)
    assert identity is not None
    assert identity["cloaked"] is cloaked
    assert (identity["image"], identity["root"], identity["minimized"]) == (
        "chrome.exe",
        True,
        False,
    )


def test_a_handle_that_is_no_longer_a_window_has_no_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(windows, "is_window", lambda _h: False)
    assert windows.window_identity(0x30) is None


class _FakeGet:
    """`DwmGetWindowAttribute` answering a fixed HRESULT and cloak value; takes the argtypes the
    probe sets."""

    def __init__(self, hresult: int, value: int) -> None:
        self.hresult, self.value = hresult, value

    def __call__(self, _hwnd: int, attribute: int, out: Any, size: int) -> int:
        # Any: the ctypes byref() argument the probe passes; `_obj` is the DWORD behind it
        assert (attribute, size) == (14, 4), "DWMWA_CLOAKED into a DWORD"
        out._obj.value = self.value
        return self.hresult


@pytest.mark.parametrize(
    ("hresult", "value", "expected"),
    [
        (0, 0, False),
        (0, 0x2, True),  # DWM_CLOAKED_SHELL: another virtual desktop
        (-0x7FF8FFFA, 0, None),  # E_HANDLE 0x80070006, measured for a dead handle
        (-0x7FF8FFA9, 0, None),  # E_INVALIDARG 0x80070057
        (1, 0x2, None),  # S_FALSE: not S_OK, so not an answer (CU-23)
        (-0x7FFFBFFB, 0x2, None),  # E_FAIL 0x80004005 with the buffer written
    ],
)
def test_is_cloaked_reads_a_failed_call_as_unclear(
    monkeypatch: pytest.MonkeyPatch, hresult: int, value: int, expected: bool | None
) -> None:
    """Issue #48: a failed read was "not cloaked", i.e. visible; unclear means fail."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        ctypes,
        "WinDLL",
        lambda *_a, **_k: SimpleNamespace(DwmGetWindowAttribute=_FakeGet(hresult, value)),
        raising=False,  # WinDLL exists only on Windows
    )
    assert windows.is_cloaked(0x30) is expected


def _fake_user32(desk: dict[int, tuple[str, int]]) -> SimpleNamespace:
    """`user32` for `list_top_windows`: enumerates `desk` (hwnd -> title, pid) in order, every
    window visible and restored; takes the argtypes the probe sets."""

    def enum_windows(callback: Any, _lparam: int) -> int:
        # Any: the probe's EnumWindows callback (WINFUNCTYPE is patched to hand it back as is)
        for hwnd in desk:
            callback(hwnd, 0)
        return 1

    def get_text(hwnd: int, buffer: Any, _size: int) -> int:
        # Any: the ctypes unicode buffer the probe passes
        buffer.value = desk[hwnd][0]
        return len(desk[hwnd][0])

    def get_pid(hwnd: int, out: Any) -> int:
        # Any: the ctypes byref() argument; `_obj` is the DWORD behind it
        out._obj.value = desk[hwnd][1]
        return 1

    return SimpleNamespace(
        EnumWindows=enum_windows,
        IsWindowVisible=lambda _h: 1,
        GetWindowTextLengthW=lambda h: len(desk[h][0]),
        GetWindowTextW=get_text,
        GetWindowThreadProcessId=get_pid,
        IsIconic=lambda _h: 0,
    )


def test_a_window_whose_cloak_cannot_be_read_is_listed_flagged_and_a_cloaked_one_is_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #48 (CU-03/06/07): DWM's None is listed, so a before/after diff never sees a change
    that did not happen, but flagged so it is never a target; True is skipped; False is plain."""
    desk = {
        0x10: ("Inbox - Google Chrome", 500),
        0x20: ("Untitled - Notepad", 600),
        0x30: ("Settings", 700),
    }
    cloak: dict[int, bool | None] = {0x10: False, 0x20: None, 0x30: True}
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_a, **_k: _fake_user32(desk), raising=False)
    monkeypatch.setattr(ctypes, "WINFUNCTYPE", lambda *_t: lambda fn: fn, raising=False)
    monkeypatch.setattr(windows, "is_cloaked", lambda h: cloak[h])
    listed = windows.list_top_windows()
    assert [(w.hwnd, w.pid, w.cloak_unreadable) for w in listed] == [
        (0x10, 500, False),
        (0x20, 600, True),
    ]


UNCLEAR = windows.WindowInfo(
    hwnd=0x20, title="New Tab - Google Chrome", pid=500, cloak_unreadable=True
)
READABLE = windows.WindowInfo(hwnd=0x10, title="New Tab - Google Chrome", pid=500)


def test_find_window_never_returns_a_window_whose_cloak_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #48: the first title match is unclear, so the readable one behind it is found."""
    monkeypatch.setattr(windows, "list_top_windows", lambda: [UNCLEAR, READABLE])
    assert windows.find_window("new tab") is READABLE
    monkeypatch.setattr(windows, "list_top_windows", lambda: [UNCLEAR])
    assert windows.find_window("new tab") is None


def _no_sleep(_s: float) -> None:
    raise AssertionError("a zero timeout never sleeps")


def test_wait_window_for_pid_never_matches_a_window_whose_cloak_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #48: an unclear window of the pid is skipped; alone, it times out."""
    monkeypatch.setattr(windows, "time", SimpleNamespace(monotonic=lambda: 100.0, sleep=_no_sleep))
    monkeypatch.setattr(windows, "list_top_windows", lambda: [UNCLEAR, READABLE])
    assert windows.wait_window_for_pid(500, timeout_s=0.0) is READABLE
    monkeypatch.setattr(windows, "list_top_windows", lambda: [UNCLEAR])
    with pytest.raises(TimeoutError, match=r"^no visible window for pid 500 within 0\.0 s$"):
        windows.wait_window_for_pid(500, timeout_s=0.0)
