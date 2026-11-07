"""`process_alive` reads an access-denied process as running (issue #69): on this machine 140
of 285 running processes refuse PROCESS_QUERY_LIMITED_INFORMATION (error 5), so reading the
refusal as "dead" called half the live processes dead. Only ERROR_INVALID_PARAMETER (87, no
such process: measured for dead pids and pid 0) is gone; any other failure is unknown, which
counts as running (unclear means held)."""

from __future__ import annotations

import ctypes
import os
import sys
from types import SimpleNamespace

import pytest

from steerai.probes import windows


class _Kernel:
    def __init__(self, *, opened: bool, exit_code: int = 259, read: bool = True) -> None:
        self.opened, self.exit_code, self.read = opened, exit_code, read

    def handles(self) -> SimpleNamespace:
        kernel = self

        class _Fn:
            def __init__(self, fn: object) -> None:
                self.fn = fn

            def __call__(self, *args: object) -> object:
                return self.fn(*args)  # type: ignore[operator]  # fn is one of the lambdas below

        def get_exit(_h: object, out: object) -> int:
            if not kernel.read:
                return 0
            out._obj.value = kernel.exit_code  # type: ignore[attr-defined]  # the byref() DWORD
            return 1

        return SimpleNamespace(
            OpenProcess=_Fn(lambda *_a: 7 if kernel.opened else None),
            GetExitCodeProcess=_Fn(get_exit),
            CloseHandle=_Fn(lambda _h: 1),
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
    kernel = _Kernel(opened=opened, exit_code=exit_code, read=read).handles()
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
