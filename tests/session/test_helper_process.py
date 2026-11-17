"""[V2.60] hook helper, PR 2: the real launch on a Windows desktop (CI only; ask the coordinator
for a slot before running locally). It installs real low-level hooks and a real hot key in the
helper; it sends no input.

- P-trampoline (permanent, rev 2a): the interpreter `helper_launch` resolves starts as one
  process, in the venv, with `steerai` importable under `-I` and the minimal environment.
- A real helper starts inside its job, reports ready with its own pid, and quits when closed."""

from __future__ import annotations

import ctypes
import os
import secrets
import subprocess
import sys
from collections.abc import Callable

import pytest

from steerai.errors import SteerError
from steerai.session.helper_core import EXIT_OK
from steerai.session.helper_launch import (
    SYNCHRONIZE,
    HelperProcess,
    WindowsLaunch,
    environment_block,
    this_interpreter,
)
from steerai.session.hook_classifier import V1_TAG
from steerai.session.hook_layout import STATE_READY

pytestmark = pytest.mark.windows

_MOD_ALT, _MOD_CTRL, _MOD_SHIFT = 0x0001, 0x0002, 0x0004
_VK_F10 = 0x79
"""A chord no Steer run uses, so a test helper never collides with a real one."""
_PROCESS_QUERY_LIMITED = 0x1000
"""PROCESS_QUERY_LIMITED_INFORMATION: to read the exit code after close."""


def _env_dict(block: str) -> dict[str, str]:
    pairs = (entry.partition("=") for entry in block.split("\0") if entry)
    return {name: value for name, _, value in pairs}


def test_p_trampoline_the_resolved_interpreter_is_one_process_in_the_venv() -> None:
    interpreter, launcher = this_interpreter()
    env = _env_dict(environment_block(system_root=os.environ["SYSTEMROOT"], launcher=launcher))
    code = "import os, sys, steerai; print(os.getpid()); print(sys.prefix); print('ok')"
    child = subprocess.Popen(  # noqa: S603 - a fixed argument list, no shell
        [str(interpreter), "-I", "-c", code],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    out, _ = child.communicate(timeout=60)
    lines = out.split()
    assert lines[-1] == "ok", out
    assert int(lines[0]) == child.pid  # one process: no trampoline in between
    assert os.path.normcase(lines[1]) == os.path.normcase(sys.prefix)


def _run_tag() -> int:
    while True:
        tag = secrets.randbits(32)
        if tag not in {0, V1_TAG}:
            return tag


def test_a_real_helper_starts_inside_its_job_reports_ready_and_quits(
    record_property: Callable[[str, object], None],
) -> None:
    """#375 P6: in the helper's own job (not any job), and the quit path ends it cleanly
    (`EXIT_OK`), not a terminate."""
    interpreter, launcher = this_interpreter()
    win32 = WindowsLaunch()
    helper = HelperProcess(
        win32,
        interpreter=interpreter,
        launcher=launcher,
        system_root=os.environ["SYSTEMROOT"],
        run_tag=_run_tag(),
        probe_vk=0x0E,
        kill_mods=_MOD_CTRL | _MOD_ALT | _MOD_SHIFT,
        kill_vk=_VK_F10,
        hook_timeout_ms=1000,
    )
    helper.start()
    watch = win32.duplicate(helper.process_handle, SYNCHRONIZE | _PROCESS_QUERY_LIMITED)
    try:
        assert helper.status.get("state") == STATE_READY
        assert helper.status.get("pid") == win32.process_id(helper.process_handle)
        record_property("ws_floor", helper.status.get("ws_floor"))
        in_job = ctypes.c_int()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.IsProcessInJob.argtypes = (ctypes.c_void_p, ctypes.c_void_p,
                                            ctypes.POINTER(ctypes.c_int))  # fmt: skip
        kernel32.IsProcessInJob.restype = ctypes.c_int
        assert kernel32.IsProcessInJob(helper.process_handle, helper.job_handle,
                                       ctypes.byref(in_job))  # fmt: skip
        assert in_job.value
    finally:
        helper.close()
    try:
        assert win32.wait_any((watch,), 3000) == 0  # the helper is gone after close()
        assert win32.exit_code(watch) == EXIT_OK  # it quit on `quit`, it was not terminated
    finally:
        win32.close(watch)


_VK_F11 = 0x7A
_MOD_NOREPEAT = 0x4000


def test_a_chord_already_held_refuses_the_helper_before_it_is_ready() -> None:
    """#385 HH4-09: ready is published only after `RegisterHotKey` succeeds. With the chord
    already registered here, the helper fails at the hot key step (`kill_key_unavailable`,
    only reachable from the failed state), so it never reported ready."""
    from ctypes import wintypes  # noqa: PLC0415 - Windows only

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.RegisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
    user32.RegisterHotKey.restype = wintypes.BOOL
    user32.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
    user32.UnregisterHotKey.restype = wintypes.BOOL
    mods = _MOD_CTRL | _MOD_ALT | _MOD_SHIFT
    assert user32.RegisterHotKey(None, 0x5EED, mods | _MOD_NOREPEAT, _VK_F11)
    try:
        interpreter, launcher = this_interpreter()
        helper = HelperProcess(
            WindowsLaunch(),
            interpreter=interpreter,
            launcher=launcher,
            system_root=os.environ["SYSTEMROOT"],
            run_tag=_run_tag(),
            probe_vk=0x0E,
            kill_mods=mods,
            kill_vk=_VK_F11,
            hook_timeout_ms=1000,
        )
        with pytest.raises(SteerError, match="kill_key_unavailable"):
            helper.start()
    finally:
        user32.UnregisterHotKey(None, 0x5EED)


def test_a_real_helper_proves_two_numbered_probes_by_their_own_numbers() -> None:
    """#492, PS-06 (CI only): two probes through `bound_probe`, distinct numbers, each seen on
    both hooks of the real helper, nothing lost."""
    import time  # noqa: PLC0415 - this test only

    from steerai.loop2.live_world import bound_probe  # noqa: PLC0415
    from steerai.probes.win_input import key_up_probe, nudge_in_place  # noqa: PLC0415
    from steerai.session.helper_monitor import _WordCounts  # noqa: PLC0415
    from steerai.session.hook_classifier import ProbeTags  # noqa: PLC0415
    from steerai.session.probe_ledger import ProbeLedger  # noqa: PLC0415

    interpreter, launcher = this_interpreter()
    tag = _run_tag()
    helper = HelperProcess(
        WindowsLaunch(),
        interpreter=interpreter,
        launcher=launcher,
        system_root=os.environ["SYSTEMROOT"],
        run_tag=tag,
        probe_vk=0x0E,
        kill_mods=_MOD_CTRL | _MOD_ALT | _MOD_SHIFT,
        kill_vk=_VK_F10,
        hook_timeout_ms=1000,
    )
    helper.start()
    try:
        counts = _WordCounts(helper, 10_000_000)
        ledger = ProbeLedger(per_hook=True, deadline_s=1.0)
        probe = bound_probe(vk=0x0E, nudge=nudge_in_place, key_up=key_up_probe)
        numbers = []
        tags = ProbeTags(tag)

        def begin() -> int:
            drawn = tags.draw()
            helper.config.set("probe_tag", drawn)  # before the probe is injected
            return drawn

        for _ in range(2):
            sent = ledger.probe_sent(now=time.monotonic(), begin=begin)
            numbers.append(sent)
            probe(sent)
            deadline = time.monotonic() + 2.0
            while not ledger.probe_seen(counts=counts) and time.monotonic() < deadline:
                time.sleep(0.02)
            assert ledger.probe_seen(counts=counts)
            assert ledger.lost_hooks(now=time.monotonic() + 5.0, counts=counts) == ()
        assert numbers[0] != numbers[1]
    finally:
        helper.close()
