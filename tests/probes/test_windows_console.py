"""The runner must never target its own terminal (RN-15), Windows Terminal included."""

from __future__ import annotations

import ctypes
import os
import sys

import pytest

from steerai.probes.windows import console_exclusions, parent_pids, window_pid

pytestmark = pytest.mark.windows


def test_exclusions_cover_this_process_its_parents_and_the_console_owner() -> None:
    hwnds, pids = console_exclusions()
    assert os.getpid() in pids
    assert os.getppid() in pids, "the parent chain is excluded"
    assert 0 not in pids
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetConsoleWindow.restype = ctypes.c_void_p
    console = kernel32.GetConsoleWindow()
    if not console:
        pytest.skip("no console attached (CI runner without a console window)")
    assert int(console) in hwnds
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetAncestor.argtypes = (ctypes.c_void_p, ctypes.c_uint)
    user32.GetAncestor.restype = ctypes.c_void_p
    owner = user32.GetAncestor(console, 3)
    if owner:
        assert int(owner) in hwnds, "the terminal window that owns the ConPTY console"
        assert window_pid(int(owner)) in pids


def test_parent_pids_walks_the_chain_nearest_first() -> None:
    chain = parent_pids(os.getpid())
    assert chain[:1] == [os.getppid()]
    assert len(chain) == len(set(chain))
    assert parent_pids(0) == []
    assert sys.platform == "win32"


@pytest.mark.windows
def test_the_desktop_window_is_the_shells_program_manager() -> None:
    """The `--desktop` start (packet open-multiwindow-2026-11-06, scope item 6)."""
    from steerai.probes.windows import desktop_window, process_name  # noqa: PLC0415

    desktop = desktop_window()
    assert desktop.title == "Program Manager"
    assert desktop.hwnd != 0
    assert process_name(desktop.pid).lower() == "explorer.exe"


@pytest.mark.windows
def test_this_interactive_session_reads_as_unlocked() -> None:
    """Issue #31: the lock flag of the session the tests run in (measured 1 = unlocked locally,
    2026-11-06); a runner whose session is not interactive would read None and stop every task."""
    from steerai.probes.windows import session_locked  # noqa: PLC0415

    assert session_locked() is False


@pytest.mark.windows
def test_a_live_windows_identity_reads_visible_root_and_not_minimized() -> None:
    """Step-window SW-12/14: the fields selection requires, read from a real window."""
    from steerai.probes.windows import list_top_windows, window_identity  # noqa: PLC0415

    window = next(w for w in list_top_windows() if not w.minimized)
    identity = window_identity(window.hwnd)
    assert identity is not None
    assert identity["visible"] is True
    assert identity["root"] is True
    assert identity["minimized"] is False
