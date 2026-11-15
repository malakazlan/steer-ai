"""Starting and stopping the hook helper ([V2.60], packet v2-hook-helper rev 2b §2.1-§2.3, §3.2,
§3.3, §8). Steer's side only; the helper is `hook_helper`.

- **The interpreter (HH-06):** the base interpreter, never the venv's `python.exe`, which is a
  trampoline that starts a second process (meas 2026-11-15). The venv is named to it by
  `__PYVENV_LAUNCHER__` in a minimal Unicode environment block (`SystemRoot` and that).
- **The job:** unnamed, kill-on-close, one active process, no breakaway. The helper is created
  inside it with `PROC_THREAD_ATTRIBUTE_JOB_LIST`; without that, created suspended, assigned,
  then resumed, so it never runs outside the job. The job handle is never inherited.
- **The handles:** exactly five are inherited (a read-only config, the status section, quit,
  wake, and Steer's own process with `SYNCHRONIZE | PROCESS_TERMINATE` for [V2.60] Q1's last
  kill layer), inheritable only around the creation, under a lock. Every kernel object is
  created without a name (§8, tested) and with a current-user-only DACL.
- **No standard handles** are given (an accepted deviation): the helper publishes every failure
  in its status words instead (`hook_helper.run_helper`).
- **Ready:** the helper's state, its pid against the process handle, and the nonce echo; a
  failure, an exit or a timeout is a refusal (K16) and the job is terminated.
"""

from __future__ import annotations

import ctypes
import ntpath
import os
import secrets
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path
from typing import Any, Final, Protocol

from steerai.errors import ErrorCode, SteerError
from steerai.session.helper_core import HelperHandles, helper_argv, write_config
from steerai.session.hook_classifier import DEAD_BAND_PX
from steerai.session.hook_layout import (
    ERROR_HOTKEY,
    LAYOUT_VERSION,
    STATE_FAILED,
    STATE_READY,
    STATES,
    Words,
    config_size,
    config_words,
    status_size,
    status_words,
)

EXTENDED_STARTUPINFO_PRESENT: Final = 0x0008_0000
CREATE_NO_WINDOW: Final = 0x0800_0000
CREATE_UNICODE_ENVIRONMENT: Final = 0x0000_0400
CREATE_SUSPENDED: Final = 0x0000_0004
FILE_MAP_WRITE: Final = 0x0002
FILE_MAP_READ: Final = 0x0004
SYNCHRONIZE: Final = 0x0010_0000
PROCESS_TERMINATE: Final = 0x0001
IMAGE_FILE_MACHINE_UNKNOWN: Final = 0
IMAGE_FILE_MACHINE_AMD64: Final = 0x8664
HEARTBEAT_MS: Final = 100
"""Rev 2 §5.3: the helper's loop period (provisional; P7 measures its cost)."""
START_TIMEOUT_MS: Final = 5000
"""Rev 2 §2.3: the in-process monitor's ready wait (P5 sets the helper's budget)."""
CLOSE_TIMEOUT_MS: Final = 2000
"""Rev 2 §2.3: the in-process monitor's close bound."""

_INHERIT_LOCK = threading.Lock()
"""Handles are inheritable only inside this lock, around one process creation."""


def _refuse(why: str) -> SteerError:
    return SteerError(ErrorCode.GATE_BLOCKED, f"monitor_unavailable: {why}")


def interpreter_and_launcher(
    *,
    prefix: str,
    base_prefix: str,
    executable: str,
    base_executable: str,
    pyvenv_cfg: str | None,
) -> tuple[Path, Path | None]:
    """HH-06: the interpreter to start and the venv's launcher to name to it (None outside a
    venv). In a venv, the base interpreter must be `pyvenv.cfg`'s `home`, or this refuses."""
    if ntpath.normcase(prefix) == ntpath.normcase(base_prefix):
        return Path(executable), None
    home = None
    for line in (pyvenv_cfg or "").removeprefix("\ufeff").splitlines():  # HL-14: a BOM
        key, sep, value = line.partition("=")
        if sep and key.strip().lower() == "home":
            home = value.strip()
    if home is None:
        raise _refuse("the venv's pyvenv.cfg names no home interpreter")
    expected = ntpath.join(home, "python.exe")
    if _real(expected) != _real(base_executable):  # HL-13: one real path on both sides
        raise _refuse("the venv's home and the base interpreter disagree")
    return Path(base_executable), Path(executable)


def _real(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def this_interpreter(
    *,
    prefix: str = sys.prefix,
    base_prefix: str = sys.base_prefix,
    executable: str = sys.executable,
    base_executable: str = getattr(sys, "_base_executable", sys.executable),
) -> tuple[Path, Path | None]:
    """`interpreter_and_launcher` for the running Steer. HL-04: a `pyvenv.cfg` that cannot be
    read or decoded is a refusal, never an exception out of the start."""
    cfg = Path(prefix) / "pyvenv.cfg"
    try:
        text = cfg.read_text(encoding="utf-8") if cfg.exists() else None
    except (OSError, UnicodeDecodeError) as exc:
        raise _refuse(f"the venv's pyvenv.cfg could not be read ({type(exc).__name__})") from exc
    return interpreter_and_launcher(
        prefix=prefix,
        base_prefix=base_prefix,
        executable=executable,
        base_executable=base_executable,
        pyvenv_cfg=text,
    )


def environment_block(*, system_root: str, launcher: Path | None) -> str:
    """The helper's whole environment: `SystemRoot`, and the venv's launcher when there is one;
    sorted by name (CreateProcessW's environment rules), double-null terminated."""
    entries = {"SystemRoot": system_root}
    if launcher is not None:
        entries["__PYVENV_LAUNCHER__"] = str(launcher)
    ordered = sorted(entries.items(), key=lambda kv: kv[0].upper())
    return "".join(f"{k}={v}\0" for k, v in ordered) + "\0"


def creation_flags(*, suspended: bool) -> int:
    """Rev 2a: `CREATE_UNICODE_ENVIRONMENT` for the Unicode block (CreateProcessW docs)."""
    flags = EXTENDED_STARTUPINFO_PRESENT | CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT
    return flags | CREATE_SUSPENDED if suspended else flags


def machine_ok(*, pointer_size: int, process_machine: int, native_machine: int) -> bool:
    """HH-05: a 64-bit interpreter, not under WOW64, on a native x64 machine (an x64 process
    emulated on ARM64 reports an ARM64 native machine)."""
    return (
        pointer_size == 8
        and process_machine == IMAGE_FILE_MACHINE_UNKNOWN
        and native_machine == IMAGE_FILE_MACHINE_AMD64
    )


class LaunchWin32(Protocol):
    """Every Win32 call the launch makes, so the sequence runs over a fake in tests."""

    def process_machine(self) -> tuple[int, int]: ...
    def user_security(self) -> object: ...
    def create_job(self, sa: object) -> int: ...
    def limit_job(self, job: int) -> None: ...
    def create_section(self, sa: object, size: int) -> int: ...
    def map_view(self, section: int, size: int, *, writable: bool) -> memoryview: ...
    def unmap(self, view: memoryview) -> None: ...
    def create_event(self, sa: object, *, manual_reset: bool) -> int: ...
    def duplicate(self, handle: int, access: int) -> int: ...
    def duplicate_self(self, access: int) -> int: ...
    def set_inherit(self, handle: int, on: bool) -> None: ...
    def create_process(  # noqa: PLR0913 - the CreateProcessW inputs
        self,
        app: str,
        *,
        cmdline: str,
        env: str,
        flags: int,
        inherit: tuple[int, ...],
        jobs: tuple[int, ...],
    ) -> tuple[int, int, int]: ...
    def assign(self, job: int, process: int) -> None: ...
    def resume(self, thread: int) -> None: ...
    def reset_event(self, handle: int) -> None: ...
    def set_event(self, handle: int) -> None: ...
    def wait_any(self, handles: tuple[int, ...], timeout_ms: int) -> int | None: ...
    def process_id(self, process: int) -> int: ...
    def has_exited(self, process: int) -> bool: ...
    def exit_code(self, process: int) -> int | None: ...
    def terminate_job(self, job: int) -> bool: ...
    def terminate_process(self, process: int) -> bool: ...
    def close(self, handle: int) -> None: ...


class HelperProcess:
    """One hook helper, started once and closed once (a v2 process runs one task)."""

    def __init__(  # noqa: PLR0913 - the helper's whole configuration, keyword-only
        self,
        win32: LaunchWin32,
        *,
        interpreter: Path,
        launcher: Path | None,
        system_root: str,
        run_tag: int,
        probe_vk: int,
        kill_mods: int,
        kill_vk: int,
        hook_timeout_ms: int,
        heartbeat_ms: int = HEARTBEAT_MS,
        dead_band_px: int = DEAD_BAND_PX,
        start_timeout_ms: int = START_TIMEOUT_MS,
        pointer_size: int | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._w = win32
        self._interpreter = interpreter
        self._launcher = launcher
        self._system_root = system_root
        self._values = {
            "run_tag": run_tag,
            "probe_vk": probe_vk,
            "kill_mods": kill_mods,
            "kill_vk": kill_vk,
            "dead_band_px": dead_band_px,
            "heartbeat_ms": heartbeat_ms,
            "hook_timeout_ms": hook_timeout_ms,
        }
        self._start_timeout_ms = start_timeout_ms
        self._pointer_size = struct.calcsize("P") if pointer_size is None else pointer_size
        self._clock = clock
        self._owned: list[int] = []
        self._views: list[memoryview] = []
        self._job: int | None = None
        self._process: int | None = None
        self._quit = 0
        self._wake = 0
        self._config: Words | None = None
        self._status: Words | None = None
        self._closed = False
        self._close_lock = threading.Lock()
        """#375 H3 (HL-19): one close at a time; a second waits until the first has released."""

    # ---- what Steer reads ---------------------------------------------------------------------

    @property
    def status(self) -> Words:
        """The helper's words, mapped read-only."""
        if self._status is None:
            raise RuntimeError("the helper is not started")
        return self._status

    @property
    def config(self) -> Words:
        """Steer's own words: `ask_depth` and `kill_ack` are written here (rev 2b §3.2, §6.1)."""
        if self._config is None:
            raise RuntimeError("the helper is not started")
        return self._config

    @property
    def wake_handle(self) -> int:
        return self._wake

    @property
    def job_handle(self) -> int:
        """The helper's own job (read-only): for the windows test's `IsProcessInJob` (#375 P6)."""
        if self._job is None:
            raise RuntimeError("the helper is not started")
        return self._job

    @property
    def process_handle(self) -> int:
        if self._process is None:
            raise RuntimeError("the helper is not started")
        return self._process

    def pid(self) -> int:
        return self._w.process_id(self.process_handle)

    def has_exited(self) -> bool:
        return self._w.has_exited(self.process_handle)

    def wait_exit(self, timeout_ms: int) -> bool:
        """The watchdog's wait: True when the helper exited within `timeout_ms`."""
        return self._w.wait_any((self.process_handle,), timeout_ms) == 0

    def wait_wake(self, timeout_ms: int) -> None:
        """HH-08, HH-15: a kill-aware wait wakes on `wake` or the helper's exit; `wake` is a
        hint only, reset once woken, and the caller re-reads the words."""
        self._w.wait_any((self._wake, self.process_handle), timeout_ms)
        self._w.reset_event(self._wake)

    def terminate(self) -> None:
        """P4 (rev 2): a helper-caused loss ends the helper at once: through its job, else its
        process, then a bounded wait for it to be gone (#375 H5)."""
        self._end()

    # ---- lifecycle ------------------------------------------------------------------------------

    def start(self) -> None:
        """Start the helper and wait until it is ready; any failure is a refusal and nothing
        is left running or open (K16)."""
        try:
            process_machine, native_machine = self._w.process_machine()
        except OSError as exc:
            raise _refuse("the machine type could not be read") from exc
        if not machine_ok(
            pointer_size=self._pointer_size,
            process_machine=process_machine,
            native_machine=native_machine,
        ):
            raise _refuse("the hook helper runs on native x64 only")
        try:
            self._start()
        except BaseException as exc:  # #375 P2: whatever went wrong, nothing is left (K16)
            self._abort()
            if isinstance(exc, SteerError) or not isinstance(exc, Exception):
                raise  # a refusal already, or an interrupt that must stay one
            raise _refuse(f"{type(exc).__name__} while starting the hook helper") from exc

    def close(self) -> None:
        """Ask the helper to quit, wait a bounded time, then end its job; idempotent, and a
        second call returns only once the first has released everything."""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            w = self._w
            process = self._process
            try:
                if process is not None and not w.has_exited(process):
                    w.set_event(self._quit)
                    w.wait_any((process,), CLOSE_TIMEOUT_MS)
                    if not w.has_exited(process):
                        self._end()
            finally:
                self._release()  # #375 P3: whatever the wait did

    # ---- internals -----------------------------------------------------------------------------

    def _own(self, handle: int) -> int:
        self._owned.append(handle)
        return handle

    def _start(self) -> None:
        w = self._w
        sa = w.user_security()
        self._job = self._own(w.create_job(sa))
        w.limit_job(self._job)
        config_section = self._own(w.create_section(sa, config_size()))
        status_section = self._own(w.create_section(sa, status_size()))
        config_view = w.map_view(config_section, config_size(), writable=True)
        self._views.append(config_view)
        self._config = config_words(config_view)
        write_config(
            self._config,
            **self._values,
            nonce=(secrets.randbits(64), secrets.randbits(64)),
        )
        self._quit = self._own(w.create_event(sa, manual_reset=True))
        self._wake = self._own(w.create_event(sa, manual_reset=True))
        handles = HelperHandles(
            config=self._own(w.duplicate(config_section, FILE_MAP_READ)),
            status=status_section,
            quit=self._quit,
            wake=self._wake,
            parent=self._own(w.duplicate_self(SYNCHRONIZE | PROCESS_TERMINATE)),
        )
        self._create(handles)
        status_ro = self._own(w.duplicate(status_section, FILE_MAP_READ))
        status_view = w.map_view(status_ro, status_size(), writable=False)
        self._views.append(status_view)
        self._status = status_words(status_view)
        for spare in (handles.config, handles.parent, status_ro, status_section, config_section):
            self._close(spare)  # the helper has its own copies; the views keep the sections
        self._wait_ready()

    def _create(self, handles: HelperHandles) -> None:
        w = self._w
        job = self._job
        if job is None:
            raise RuntimeError("the job is created before the helper")
        app = str(self._interpreter)
        cmdline = subprocess.list2cmdline([app, *helper_argv(handles, LAYOUT_VERSION)])
        env = environment_block(system_root=self._system_root, launcher=self._launcher)
        inherit = handles.all()
        with _INHERIT_LOCK:
            try:
                for h in inherit:  # #375 H1: inside the try, so a failure part-way is undone
                    w.set_inherit(h, True)
                try:
                    process, thread, _pid = w.create_process(
                        app,
                        cmdline=cmdline,
                        env=env,
                        flags=creation_flags(suspended=False),
                        inherit=inherit,
                        jobs=(job,),
                    )
                    suspended = False
                except OSError:  # P4: no JOB_LIST here; create suspended, assign, resume
                    process, thread, _pid = w.create_process(
                        app,
                        cmdline=cmdline,
                        env=env,
                        flags=creation_flags(suspended=True),
                        inherit=inherit,
                        jobs=(),
                    )
                    suspended = True
            finally:
                self._no_inherit(inherit)
        self._process = self._own(process)
        self._own(thread)
        if suspended:
            try:
                w.assign(job, process)
            except OSError:
                w.terminate_process(process)
                raise
            w.resume(thread)
        self._close(thread)

    def _no_inherit(self, handles: tuple[int, ...]) -> None:
        """#375 H1: every handle is made non-inheritable again, each attempted even when one
        fails; the first failure is raised once all were tried."""
        failure: OSError | None = None
        for h in handles:
            try:
                self._w.set_inherit(h, False)
            except OSError as exc:
                failure = failure or exc
        if failure is not None:
            raise failure

    def _wait_ready(self) -> None:
        w = self._w
        status = self.status
        process = self.process_handle
        deadline = self._clock() + self._start_timeout_ms / 1000
        while True:
            w.reset_event(self._wake)
            state = status.get("state")
            if state not in STATES:  # #375 H7 (HL-21): not a state the helper can write
                raise _refuse(f"the hook helper reported an unknown state ({state})")
            if state == STATE_READY:
                if w.has_exited(process):  # #375 H2 (HL-12): ready, then gone
                    raise _refuse("the hook helper exited after reporting ready")
                self._check_identity()
                return
            if state == STATE_FAILED:
                error = status.get("error")
                if error == ERROR_HOTKEY:
                    raise SteerError(
                        ErrorCode.GATE_BLOCKED,
                        "kill_key_unavailable: another program holds the kill chord; pass "
                        "--kill-key",
                    )
                raise _refuse(f"the hook helper failed (error {error})")
            if w.has_exited(process):
                raise _refuse(f"the hook helper exited (code {w.exit_code(process)})")
            remaining_ms = int((deadline - self._clock()) * 1000)
            if remaining_ms <= 0 or w.wait_any((self._wake, process), remaining_ms) is None:
                raise _refuse("the hook helper did not become ready in time")

    def _check_identity(self) -> None:
        """The words were written by the helper this run: its pid and the run's nonce."""
        status, config = self.status, self.config
        pid = self._w.process_id(self.process_handle)
        if pid == 0:  # #375 H8: GetProcessId failed; no identity to compare
            raise _refuse("the hook helper's process id could not be read")
        same = (
            status.get("pid") == pid
            and status.get("nonce_echo_lo") == config.get("nonce_lo")
            and status.get("nonce_echo_hi") == config.get("nonce_hi")
        )
        if not same:
            raise _refuse("the hook helper's identity did not match")

    def _abort(self) -> None:
        self._closed = True
        try:
            self._end()
        finally:
            self._release()

    def _end(self) -> None:
        """#375 H5 (HL-09): end the helper through its job, else its process (each result
        checked), then wait a bounded time for it to be gone, so a restart never meets it still
        holding the kill chord."""
        w = self._w
        ended = self._job is not None and w.terminate_job(self._job)
        if not ended and self._process is not None:
            w.terminate_process(self._process)
        if self._process is not None:
            w.wait_any((self._process,), CLOSE_TIMEOUT_MS)

    def _close(self, handle: int) -> None:
        if handle in self._owned:
            self._owned.remove(handle)
            self._w.close(handle)

    def _release(self) -> None:
        self._status = None  # #375 P4: no `Words` over unmapped memory
        self._config = None
        while self._views:
            self._w.unmap(self._views.pop())
        while self._owned:
            self._w.close(self._owned.pop())


# ---- the real Win32 calls (Windows only; covered by the windows-marked tests) -------------------

PAGE_READWRITE: Final = 0x04
HANDLE_FLAG_INHERIT: Final = 0x01
JOB_OBJECT_LIMIT_ACTIVE_PROCESS: Final = 0x0008
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: Final = 0x2000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION: Final = 9
PROC_THREAD_ATTRIBUTE_HANDLE_LIST: Final = 0x0002_0002
PROC_THREAD_ATTRIBUTE_JOB_LIST: Final = 0x0002_000D
TOKEN_QUERY: Final = 0x0008
TOKEN_USER_CLASS: Final = 1
SDDL_REVISION_1: Final = 1
WAIT_OBJECT_0: Final = 0
WAIT_TIMEOUT: Final = 0x0102
STILL_ACTIVE: Final = 259


class SecurityAttributes(ctypes.Structure):
    _fields_ = (
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wintypes.BOOL),
    )


class JobBasicLimits(ctypes.Structure):
    _fields_ = (
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    )


class JobExtendedLimits(ctypes.Structure):
    _fields_ = (
        ("Basic", JobBasicLimits),
        ("IoInfo", ctypes.c_uint64 * 6),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    )


class StartupInfo(ctypes.Structure):
    _fields_ = (
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    )


class StartupInfoEx(ctypes.Structure):
    _fields_ = (("StartupInfo", StartupInfo), ("lpAttributeList", ctypes.c_void_p))


class ProcessInformation(ctypes.Structure):
    _fields_ = (
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    )


class WindowsLaunch:  # pragma: no cover - live, Windows only (tests/session windows-marked)
    """`LaunchWin32` over kernel32 and advapi32."""

    def __init__(self) -> None:
        self._k: Any = ctypes.WinDLL("kernel32", use_last_error=True)  # Any: a ctypes DLL
        self._a: Any = ctypes.WinDLL("advapi32", use_last_error=True)  # Any: a ctypes DLL
        self._maps: dict[int, int] = {}
        self._keep: list[object] = []
        self._sa: Any = None  # Any: the ctypes SECURITY_ATTRIBUTES, built once
        self._declare()

    def _declare(self) -> None:  # noqa: PLR0915 - one line per foreign function, by design
        """#375 P1: argtypes and restype for every call (Microsoft Learn signatures; HANDLE is
        pointer-width), so no handle (the pseudo-handle -1 above all) is converted as a C int."""
        k, a = self._k, self._a
        handle, dword, boolean = wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL
        sa = ctypes.POINTER(SecurityAttributes)
        k.GetCurrentProcess.argtypes = ()
        k.GetCurrentProcess.restype = handle
        if hasattr(k, "IsWow64Process2"):  # Windows 10 1709 and later
            k.IsWow64Process2.argtypes = (handle, ctypes.POINTER(wintypes.USHORT),
                                          ctypes.POINTER(wintypes.USHORT))  # fmt: skip
            k.IsWow64Process2.restype = boolean
        k.CreateJobObjectW.argtypes = (sa, wintypes.LPCWSTR)
        k.CreateJobObjectW.restype = handle
        k.SetInformationJobObject.argtypes = (handle, ctypes.c_int, ctypes.c_void_p, dword)
        k.SetInformationJobObject.restype = boolean
        k.CreateFileMappingW.argtypes = (handle, sa, dword, dword, dword, wintypes.LPCWSTR)
        k.CreateFileMappingW.restype = handle
        k.MapViewOfFile.argtypes = (handle, dword, dword, dword, ctypes.c_size_t)
        k.MapViewOfFile.restype = ctypes.c_void_p
        k.UnmapViewOfFile.argtypes = (ctypes.c_void_p,)
        k.UnmapViewOfFile.restype = boolean
        k.CreateEventW.argtypes = (sa, boolean, boolean, wintypes.LPCWSTR)
        k.CreateEventW.restype = handle
        k.DuplicateHandle.argtypes = (handle, handle, handle, ctypes.POINTER(handle), dword,
                                      boolean, dword)  # fmt: skip
        k.DuplicateHandle.restype = boolean
        k.SetHandleInformation.argtypes = (handle, dword, dword)
        k.SetHandleInformation.restype = boolean
        size_out = ctypes.POINTER(ctypes.c_size_t)
        k.InitializeProcThreadAttributeList.argtypes = (ctypes.c_void_p, dword, dword, size_out)
        k.InitializeProcThreadAttributeList.restype = boolean
        k.UpdateProcThreadAttribute.argtypes = (ctypes.c_void_p, dword, ctypes.c_size_t,
                                                ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
                                                ctypes.POINTER(ctypes.c_size_t))  # fmt: skip
        k.UpdateProcThreadAttribute.restype = boolean
        k.DeleteProcThreadAttributeList.argtypes = (ctypes.c_void_p,)
        k.DeleteProcThreadAttributeList.restype = None
        k.CreateProcessW.argtypes = (wintypes.LPCWSTR, ctypes.c_wchar_p, sa, sa, boolean, dword,
                                     ctypes.c_void_p, wintypes.LPCWSTR,
                                     ctypes.POINTER(StartupInfoEx),
                                     ctypes.POINTER(ProcessInformation))  # fmt: skip
        k.CreateProcessW.restype = boolean
        k.AssignProcessToJobObject.argtypes = (handle, handle)
        k.AssignProcessToJobObject.restype = boolean
        k.ResumeThread.argtypes = (handle,)
        k.ResumeThread.restype = dword
        k.ResetEvent.argtypes = (handle,)
        k.ResetEvent.restype = boolean
        k.SetEvent.argtypes = (handle,)
        k.SetEvent.restype = boolean
        k.WaitForMultipleObjects.argtypes = (dword, ctypes.POINTER(handle), boolean, dword)
        k.WaitForMultipleObjects.restype = dword
        k.WaitForSingleObject.argtypes = (handle, dword)
        k.WaitForSingleObject.restype = dword
        k.GetProcessId.argtypes = (handle,)
        k.GetProcessId.restype = dword
        k.GetExitCodeProcess.argtypes = (handle, ctypes.POINTER(dword))
        k.GetExitCodeProcess.restype = boolean
        k.TerminateJobObject.argtypes = (handle, wintypes.UINT)
        k.TerminateJobObject.restype = boolean
        k.TerminateProcess.argtypes = (handle, wintypes.UINT)
        k.TerminateProcess.restype = boolean
        k.CloseHandle.argtypes = (handle,)
        k.CloseHandle.restype = boolean
        k.LocalFree.argtypes = (ctypes.c_void_p,)
        k.LocalFree.restype = ctypes.c_void_p
        a.OpenProcessToken.argtypes = (handle, dword, ctypes.POINTER(handle))
        a.OpenProcessToken.restype = boolean
        a.GetTokenInformation.argtypes = (handle, ctypes.c_int, ctypes.c_void_p, dword,
                                          ctypes.POINTER(dword))  # fmt: skip
        a.GetTokenInformation.restype = boolean
        a.ConvertSidToStringSidW.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR))
        a.ConvertSidToStringSidW.restype = boolean
        a.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
            wintypes.LPCWSTR,
            dword,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_ulong),
        )
        a.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = boolean

    def _fail(self, what: str) -> OSError:
        code = ctypes.get_last_error()
        return OSError(code, f"{what} failed ({code})")

    def process_machine(self) -> tuple[int, int]:
        if not hasattr(self._k, "IsWow64Process2"):
            raise OSError(0, "IsWow64Process2 is not available (Windows 10 1709 or later)")
        process, native = wintypes.USHORT(), wintypes.USHORT()
        me = self._k.GetCurrentProcess()
        if not self._k.IsWow64Process2(me, ctypes.byref(process), ctypes.byref(native)):
            raise self._fail("IsWow64Process2")
        return int(process.value), int(native.value)

    def user_security(self) -> object:
        """SECURITY_ATTRIBUTES whose DACL grants the current user only (§8 point 2)."""
        if self._sa is not None:
            return self._sa
        token = wintypes.HANDLE()
        if not self._a.OpenProcessToken(self._k.GetCurrentProcess(), TOKEN_QUERY,
                                        ctypes.byref(token)):  # fmt: skip
            raise self._fail("OpenProcessToken")
        try:
            needed = wintypes.DWORD()
            self._a.GetTokenInformation(token, TOKEN_USER_CLASS, None, 0, ctypes.byref(needed))
            buf = ctypes.create_string_buffer(needed.value)
            if not self._a.GetTokenInformation(token, TOKEN_USER_CLASS, buf, needed,
                                               ctypes.byref(needed)):  # fmt: skip
                raise self._fail("GetTokenInformation")
        finally:
            self._k.CloseHandle(token)
        sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]  # TOKEN_USER.User.Sid
        text = wintypes.LPWSTR()
        if not self._a.ConvertSidToStringSidW(ctypes.c_void_p(sid), ctypes.byref(text)):
            raise self._fail("ConvertSidToStringSidW")
        user = text.value
        self._k.LocalFree(ctypes.cast(text, ctypes.c_void_p))
        descriptor = ctypes.c_void_p()
        if not self._a.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            f"D:P(A;;GA;;;{user})", SDDL_REVISION_1, ctypes.byref(descriptor), None
        ):
            raise self._fail("ConvertStringSecurityDescriptorToSecurityDescriptorW")

        sa = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
        self._keep.extend((buf, descriptor))
        self._sa = sa
        return sa

    def create_job(self, sa: object) -> int:
        job = self._k.CreateJobObjectW(ctypes.byref(sa), None)  # type: ignore[arg-type]  # a ctypes struct
        if not job:
            raise self._fail("CreateJobObjectW")
        return int(job)

    def limit_job(self, job: int) -> None:

        info = JobExtendedLimits()
        info.Basic.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        info.Basic.ActiveProcessLimit = 1
        limits = (ctypes.addressof(info), ctypes.sizeof(info))
        if not self._k.SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, *limits):
            raise self._fail("SetInformationJobObject")

    def create_section(self, sa: object, size: int) -> int:
        invalid = ctypes.c_void_p(-1)  # INVALID_HANDLE_VALUE: a mapping backed by the paging file
        section = self._k.CreateFileMappingW(invalid, ctypes.byref(sa), PAGE_READWRITE, 0,  # type: ignore[arg-type]  # a ctypes struct
                                             size, None)  # fmt: skip
        if not section:
            raise self._fail("CreateFileMappingW")
        return int(section)

    def map_view(self, section: int, size: int, *, writable: bool) -> memoryview:
        access = FILE_MAP_READ | (FILE_MAP_WRITE if writable else 0)
        address = self._k.MapViewOfFile(section, access, 0, 0, size)
        if not address:
            raise self._fail("MapViewOfFile")
        array = (ctypes.c_char * size).from_address(address)
        view = memoryview(array).cast("B")
        view = view if writable else view.toreadonly()
        self._maps[id(view)] = int(address)
        self._keep.append(array)
        return view

    def unmap(self, view: memoryview) -> None:
        address = self._maps.pop(id(view), None)
        view.release()
        if address is not None:
            self._k.UnmapViewOfFile(address)

    def create_event(self, sa: object, *, manual_reset: bool) -> int:
        event = self._k.CreateEventW(ctypes.byref(sa), manual_reset, False, None)  # type: ignore[arg-type]  # a ctypes struct
        if not event:
            raise self._fail("CreateEventW")
        return int(event)

    def duplicate(self, handle: int, access: int) -> int:
        me = self._k.GetCurrentProcess()
        out = wintypes.HANDLE()
        if not self._k.DuplicateHandle(me, handle, me, ctypes.byref(out), access, False, 0):
            raise self._fail("DuplicateHandle")
        return int(out.value or 0)

    def duplicate_self(self, access: int) -> int:
        return self.duplicate(int(self._k.GetCurrentProcess()), access)

    def set_inherit(self, handle: int, on: bool) -> None:
        if not self._k.SetHandleInformation(handle, HANDLE_FLAG_INHERIT,
                                            HANDLE_FLAG_INHERIT if on else 0):  # fmt: skip
            raise self._fail("SetHandleInformation")

    def create_process(  # noqa: PLR0913 - the CreateProcessW inputs
        self,
        app: str,
        *,
        cmdline: str,
        env: str,
        flags: int,
        inherit: tuple[int, ...],
        jobs: tuple[int, ...],
    ) -> tuple[int, int, int]:

        k = self._k
        count = 2 if jobs else 1
        size = ctypes.c_size_t()
        k.InitializeProcThreadAttributeList(None, count, 0, ctypes.byref(size))
        attributes = ctypes.create_string_buffer(size.value)
        if not k.InitializeProcThreadAttributeList(attributes, count, 0, ctypes.byref(size)):
            raise self._fail("InitializeProcThreadAttributeList")
        try:
            handle_list = (wintypes.HANDLE * len(inherit))(*inherit)
            if not k.UpdateProcThreadAttribute(
                attributes, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST, handle_list,
                ctypes.sizeof(handle_list), None, None,
            ):  # fmt: skip
                raise self._fail("UpdateProcThreadAttribute(HANDLE_LIST)")
            job_list = (wintypes.HANDLE * max(len(jobs), 1))(*jobs)
            if jobs and not k.UpdateProcThreadAttribute(
                attributes, 0, PROC_THREAD_ATTRIBUTE_JOB_LIST, job_list, ctypes.sizeof(job_list),
                None, None,
            ):  # fmt: skip
                raise self._fail("UpdateProcThreadAttribute(JOB_LIST)")
            startup = StartupInfoEx()
            startup.StartupInfo.cb = ctypes.sizeof(StartupInfoEx)
            startup.lpAttributeList = ctypes.cast(attributes, ctypes.c_void_p)
            info = ProcessInformation()
            command = ctypes.create_unicode_buffer(cmdline)  # CreateProcessW may write to it
            environment = ctypes.create_unicode_buffer(env)
            if not k.CreateProcessW(app, command, None, None, True, flags, environment, None,
                                    ctypes.byref(startup), ctypes.byref(info)):  # fmt: skip
                raise self._fail("CreateProcessW")
        finally:
            k.DeleteProcThreadAttributeList(attributes)
        return int(info.hProcess), int(info.hThread), int(info.dwProcessId)

    def assign(self, job: int, process: int) -> None:
        if not self._k.AssignProcessToJobObject(job, process):
            raise self._fail("AssignProcessToJobObject")

    def resume(self, thread: int) -> None:
        if self._k.ResumeThread(thread) == 0xFFFF_FFFF:
            raise self._fail("ResumeThread")

    def reset_event(self, handle: int) -> None:
        self._k.ResetEvent(handle)

    def set_event(self, handle: int) -> None:
        self._k.SetEvent(handle)

    def wait_any(self, handles: tuple[int, ...], timeout_ms: int) -> int | None:
        array = (wintypes.HANDLE * len(handles))(*handles)
        got = int(self._k.WaitForMultipleObjects(len(handles), array, False, timeout_ms))
        if got == WAIT_TIMEOUT:
            return None
        if WAIT_OBJECT_0 <= got < WAIT_OBJECT_0 + len(handles):
            return got - WAIT_OBJECT_0
        raise self._fail("WaitForMultipleObjects")

    def process_id(self, process: int) -> int:
        return int(self._k.GetProcessId(process))

    def has_exited(self, process: int) -> bool:
        return int(self._k.WaitForSingleObject(process, 0)) == WAIT_OBJECT_0

    def exit_code(self, process: int) -> int | None:
        code = wintypes.DWORD()
        if not self._k.GetExitCodeProcess(process, ctypes.byref(code)):
            return None
        return None if code.value == STILL_ACTIVE else int(code.value)

    def terminate_job(self, job: int) -> bool:
        return bool(self._k.TerminateJobObject(job, 1))

    def terminate_process(self, process: int) -> bool:
        return bool(self._k.TerminateProcess(process, 1))

    def close(self, handle: int) -> None:
        self._k.CloseHandle(handle)
