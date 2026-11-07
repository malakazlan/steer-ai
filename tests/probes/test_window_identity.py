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
