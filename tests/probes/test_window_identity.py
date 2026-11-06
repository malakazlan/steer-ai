"""`window_identity` reports the fields a selection checks, `cloaked` included (issue #38,
hunter SF-10), against fake readers so the shape is checked without a desktop."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from steerai.probes import windows


@pytest.mark.parametrize("cloaked", [False, True])
def test_the_identity_carries_whether_dwm_cloaks_the_window(
    monkeypatch: pytest.MonkeyPatch, cloaked: bool
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
