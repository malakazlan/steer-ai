"""The launcher executor for `open` (design 5.5 P1, 5.9; packet open-multiwindow-2026-11-06).

The kill blocker is asked before the launch; the argument list comes from the code-owned
registries (`steerai.action.launch`) and goes to `CreateProcess` with no shell. Then the top
windows are polled for one new window (not in the before list, not minimized) whose process image
is the app's: a browser hands the launch to its running instance, so the launched pid is never
the identity. The result is `delivered_unverified` with the new window's handle in evidence; the
loop verifies the code-owned title and only then makes that window the current one. Content
never enters the evidence.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import PurePath
from typing import Any

from steerai.action.dispositions import ActionResult, Delivery, Step
from steerai.action.launch import APPS, LaunchError, launch_argv, launch_env, resolve_exe
from steerai.errors import ErrorCode
from steerai.probes.windows import WindowInfo

OPEN_WINDOW_S = 15.0
"""How long a launch may take to show its window (unmeasured; the live test records it)."""
POLL_S = 0.1
_NO_WINDOW_FLAGS = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


@dataclass(frozen=True, slots=True)
class OpenAction:
    app: str
    site: str | None = None
    text: str | None = field(default=None, repr=False)
    verb: str = "open"


def _real_popen(argv: list[str]) -> Any:  # Any: a Popen the launcher never reads
    return subprocess.Popen(  # noqa: S603 - registry-built argv, no shell
        argv,
        env=launch_env(os.environ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=_NO_WINDOW_FLAGS,
    )


class Launcher:
    def __init__(  # noqa: PLR0913 - every system call is injected so the executor runs pure
        self,
        *,
        popen: Callable[[list[str]], Any] = _real_popen,  # Any: the process is not read
        list_windows: Callable[[], Sequence[WindowInfo]] | None = None,
        process_name: Callable[[int], str] | None = None,
        resolve_exe: Callable[[Sequence[str]], str] = resolve_exe,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        blocker: Callable[[], str | None] | None = None,
        process_started: Callable[[int], int | None] | None = None,
        filetime_now: Callable[[], int] | None = None,
    ) -> None:
        if process_started is None or filetime_now is None:
            from steerai.probes import windows as win  # noqa: PLC0415 - Win32

            process_started = process_started or win.process_start_time
            filetime_now = filetime_now or win.filetime_now
        self._process_started = process_started
        self._filetime_now = filetime_now
        if list_windows is None or process_name is None:
            from steerai.probes.windows import list_top_windows  # noqa: PLC0415 - Win32
            from steerai.probes.windows import process_name as real_process_name  # noqa: PLC0415

            list_windows = list_windows or list_top_windows
            process_name = process_name or real_process_name
        self._popen = popen
        self._list_windows = list_windows
        self._process_name = process_name
        self._resolve_exe = resolve_exe
        self._monotonic = monotonic
        self._sleep = sleep
        self._blocker = blocker

    def execute(self, action: OpenAction, windows_before: Sequence[WindowInfo]) -> ActionResult:
        try:
            spec = APPS.get(action.app)
            if spec is None:
                raise LaunchError(f"unknown app {action.app!r}")
            exe = self._resolve_exe(spec.exe_candidates)
            argv = launch_argv(action.app, action.site, action.text, exe=exe)
        except LaunchError:
            return _refused(ErrorCode.ELEMENT_NOT_ACTIONABLE, "launch_refused")
        if self._blocker is not None and (token := self._blocker()) is not None:
            return _refused(ErrorCode.GATE_BLOCKED, token)
        launch_ft = self._filetime_now()  # before the launch: a process started after is new
        try:
            self._popen(argv)
        except OSError:
            return _refused(ErrorCode.ELEMENT_NOT_ACTIONABLE, "launch_failed")
        steps = [Step(label="launch", mechanism="launch", outcome="succeeded")]
        evidence: dict[str, Any] = {"launched": PurePath(exe).name.lower()}
        launched_at = self._monotonic()
        found = self._new_window(spec.process_names, {w.hwnd for w in windows_before})
        if isinstance(found, tuple):
            # several windows: say what they were, never their titles (#75)
            fresh = found
            found = "several_new_windows"
            evidence["fresh_windows"] = [
                {
                    "z": index,  # list order is z-order, foreground first
                    "same_process": w.pid == fresh[0].pid,
                    "process_new": _started_after(self._process_started(w.pid), launch_ft),
                    "title_chars": len(w.title),
                }
                for index, w in enumerate(fresh)
            ]
            evidence["seen_after_s"] = round(self._monotonic() - launched_at, 1)
        if isinstance(found, str):
            evidence["detail"] = found
            timed_out = found in ("no_new_window", "several_new_windows")
            code = ErrorCode.VERIFY_TIMEOUT if timed_out else ErrorCode.GATE_BLOCKED
            return ActionResult(
                verb="open",
                delivery=Delivery.DELIVERY_UNCERTAIN,
                steps=steps,
                code=code,
                evidence=evidence,
            )
        evidence["hwnd"] = found.hwnd
        return ActionResult(
            verb="open",
            delivery=Delivery.DELIVERED_UNVERIFIED,
            steps=steps,
            code=ErrorCode.VERIFY_TIMEOUT,
            evidence=evidence,
        )

    def _new_window(
        self, process_names: frozenset[str], seen: set[int]
    ) -> WindowInfo | tuple[WindowInfo, ...] | str:
        """The one new, non-minimized window of the app's process; the several new ones; or a
        fixed detail token."""
        deadline = self._monotonic() + OPEN_WINDOW_S
        while True:
            fresh = [
                w
                for w in self._list_windows()
                if w.hwnd not in seen
                and not w.minimized
                and not w.cloak_unreadable  # unclear is never the new window (#48)
                and self._process_name(w.pid).lower() in process_names
            ]
            if len(fresh) == 1:
                return fresh[0]
            if len(fresh) > 1:
                return tuple(fresh)
            if self._monotonic() >= deadline:
                return "no_new_window"
            if self._blocker is not None and (token := self._blocker()) is not None:
                return token
            self._sleep(POLL_S)


def _started_after(started: int | None, launch_ft: int) -> bool | None:
    """Whether a window's process started with this launch (a session restore needs a cold
    start), None when its start cannot be read (#75)."""
    return None if started is None else started >= launch_ft


def _refused(code: ErrorCode, detail: str) -> ActionResult:
    return ActionResult(
        verb="open",
        delivery=Delivery.NOT_DELIVERED,
        steps=[],
        code=code,
        evidence={"reason": code.value, "detail": detail},
    )
