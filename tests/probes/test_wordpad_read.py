"""WordPad's text is read through its RichEdit child by WM_GETTEXT (#152 review item 1): set by
WM_SETTEXT, a window message, never input. Skipped where WordPad is absent (removed from
Windows 11 24H2; unverified here)."""

from __future__ import annotations

import ctypes
import subprocess
from ctypes import wintypes
from typing import Any

import pytest

from steerai.probes.task_suite import WORDPAD, notepad_text
from steerai.probes.windows import wait_window_for_pid

pytestmark = pytest.mark.windows


@pytest.mark.skipif(not WORDPAD.is_file(), reason="WordPad is not installed")
def test_wordpad_text_reads_back_through_its_richedit_child() -> None:
    proc = subprocess.Popen([str(WORDPAD)])  # noqa: S603 - a fixed system path
    try:
        window = wait_window_for_pid(proc.pid, image=WORDPAD.name, process=proc)
        user32: Any = ctypes.WinDLL("user32")  # Any: a ctypes library handle
        user32.FindWindowExW.restype = wintypes.HWND
        user32.FindWindowExW.argtypes = (
            wintypes.HWND,
            wintypes.HWND,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
        )
        user32.SendMessageW.argtypes = (
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPCWSTR,
        )
        edit = user32.FindWindowExW(window.hwnd, None, "RICHEDIT50W", None)
        assert edit, "RICHEDIT50W is a direct child of the WordPad frame"
        user32.SendMessageW(edit, 0x000C, 0, "quarterly plan draft")  # WM_SETTEXT
        assert notepad_text(window.hwnd, "RICHEDIT50W").strip() == "quarterly plan draft"
    finally:
        subprocess.run(  # noqa: S603 - fixed argv
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],  # noqa: S607 - taskkill on PATH
            check=False,
            capture_output=True,
        )
