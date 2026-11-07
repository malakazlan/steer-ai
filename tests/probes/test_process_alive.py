"""`process_alive` reads an access-denied process as running (issue #69): on this machine 140
of 285 running processes refuse PROCESS_QUERY_LIMITED_INFORMATION (error 5), so reading the
refusal as "dead" called half the live processes dead. Only ERROR_INVALID_PARAMETER (87, no
such process: measured for dead pids and pid 0) is gone; any other failure is unknown, which
counts as running (unclear means held)."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from steerai.probes import windows


def _kernel(*, opened: bool, exit_code: int, read: bool) -> SimpleNamespace:
    """kernel32 as `process_alive` calls it: OpenProcess, GetExitCodeProcess, CloseHandle."""

    def get_exit(_handle: object, out: Any) -> int:  # Any: the byref() DWORD the probe passes
        if not read:
            return 0
        out._obj.value = exit_code
        return 1

    return SimpleNamespace(
        OpenProcess=lambda *_a: 7 if opened else None,
        GetExitCodeProcess=get_exit,
        CloseHandle=lambda _h: 1,
    )


@pytest.mark.parametrize(
    ("opened", "error", "exit_code", "read", "alive"),
    [
        (False, 87, 259, True, False),  # no such process
        (False, 5, 259, True, True),  # access denied: it exists
        (False, 6, 259, True, True),  # anything else: unknown, held
        (True, 0, 259, True, True),  # STILL_ACTIVE
        (True, 0, 0, True, False),  # exited
        (True, 0, 259, False, True),  # exit code unreadable: unknown, held
    ],
)
def test_only_a_missing_process_is_dead(
    monkeypatch: pytest.MonkeyPatch,
    *,
    opened: bool,
    error: int,
    exit_code: int,
    read: bool,
    alive: bool,
) -> None:
    kernel = _kernel(opened=opened, exit_code=exit_code, read=read)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_a, **_k: kernel, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: error, raising=False)
    assert windows.process_alive(4242) is alive


@pytest.mark.windows
def test_the_real_system_process_is_alive_and_a_dead_pid_is_not() -> None:
    """Read-only: pid 4 refuses a query handle (error 5) yet is running; a dead pid gives 87."""
    assert windows.process_alive(4) is True
    assert windows.process_alive(os.getpid()) is True
    assert windows.process_alive(2**31 - 7) is False


@pytest.mark.windows
def test_a_child_that_exited_is_not_alive() -> None:
    """Principal review of #70: the exit-code branch on the real API. The open Popen handle keeps
    the pid from being reused, so the result is deterministic."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert windows.process_alive(child.pid) is False
