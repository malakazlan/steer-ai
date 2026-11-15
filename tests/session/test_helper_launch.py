"""[V2.60] hook helper, PR 2 (packet rev 2b §2.1-§2.3, §3.2-§3.4, §8): starting the helper.

Pure planning (the interpreter, the environment block, the command line, the machine check) and
the start/close sequence over a recording fake of the Win32 calls. The real calls are covered by
the windows-marked tests in `test_helper_process.py` (CI only)."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from steerai.errors import SteerError
from steerai.session import helper_launch
from steerai.session.helper_core import HelperHandles, config_error, parse_helper_argv
from steerai.session.helper_launch import (
    CREATE_NO_WINDOW,
    CREATE_SUSPENDED,
    CREATE_UNICODE_ENVIRONMENT,
    EXTENDED_STARTUPINFO_PRESENT,
    FILE_MAP_READ,
    PROCESS_TERMINATE,
    SYNCHRONIZE,
    HelperProcess,
    creation_flags,
    environment_block,
    interpreter_and_launcher,
    machine_ok,
)
from steerai.session.hook_layout import (
    ERROR_HOOKS,
    ERROR_HOTKEY,
    STATE_FAILED,
    STATE_READY,
    config_words,
    status_words,
)

AMD64 = 0x8664
ARM64 = 0xAA64
UNKNOWN = 0
I386 = 0x014C


# ---- pure planning ---------------------------------------------------------------------------


def test_in_a_venv_the_base_interpreter_is_launched_with_the_venv_named() -> None:
    """HH-06 (meas 2026-11-15): the venv's python.exe is a trampoline that starts a second
    process; the base interpreter with __PYVENV_LAUNCHER__ is one process in the venv."""
    exe, launcher = interpreter_and_launcher(
        prefix=r"D:\w\.venv",
        base_prefix=r"C:\py",
        executable=r"D:\w\.venv\Scripts\python.exe",
        base_executable=r"C:\py\python.exe",
        pyvenv_cfg="home = C:\\py\nimplementation = CPython\n",
    )
    assert exe == Path(r"C:\py\python.exe")
    assert launcher == Path(r"D:\w\.venv\Scripts\python.exe")


def test_outside_a_venv_the_interpreter_itself_is_launched() -> None:
    exe, launcher = interpreter_and_launcher(
        prefix=r"C:\py",
        base_prefix=r"C:\py",
        executable=r"C:\py\python.exe",
        base_executable=r"C:\py\python.exe",
        pyvenv_cfg=None,
    )
    assert (exe, launcher) == (Path(r"C:\py\python.exe"), None)


@pytest.mark.parametrize(
    "cfg",
    [None, "implementation = CPython\n", "home = C:\\other\n"],
)
def test_a_venv_whose_home_disagrees_or_is_missing_is_refused(cfg: str | None) -> None:
    with pytest.raises(SteerError, match="monitor_unavailable"):
        interpreter_and_launcher(
            prefix=r"D:\w\.venv",
            base_prefix=r"C:\py",
            executable=r"D:\w\.venv\Scripts\python.exe",
            base_executable=r"C:\py\python.exe",
            pyvenv_cfg=cfg,
        )


def test_the_environment_block_is_minimal_sorted_and_double_null_terminated() -> None:
    block = environment_block(system_root=r"C:\Windows", launcher=Path(r"D:\v\python.exe"))
    assert block == "SystemRoot=C:\\Windows\0__PYVENV_LAUNCHER__=D:\\v\\python.exe\0\0"
    assert environment_block(system_root=r"C:\Windows", launcher=None) == (
        "SystemRoot=C:\\Windows\0\0"
    )


def test_the_creation_flags() -> None:
    """Rev 2a: a Unicode block needs CREATE_UNICODE_ENVIRONMENT (CreateProcessW docs)."""
    base = EXTENDED_STARTUPINFO_PRESENT | CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT
    assert creation_flags(suspended=False) == base
    assert creation_flags(suspended=True) == base | CREATE_SUSPENDED


@pytest.mark.parametrize(
    ("pointer", "process", "native", "ok"),
    [
        (8, UNKNOWN, AMD64, True),
        (4, UNKNOWN, AMD64, False),  # a 32-bit interpreter
        (8, I386, AMD64, False),  # WOW64
        (8, UNKNOWN, ARM64, False),  # x64 emulated on ARM64
    ],
)
def test_only_native_x64_is_accepted(pointer: int, process: int, native: int, ok: bool) -> None:
    """HH-05: never `platform.machine()`, which can report the emulated machine."""
    assert machine_ok(pointer_size=pointer, process_machine=process, native_machine=native) is ok


def test_every_kernel_object_is_created_without_a_name() -> None:
    """§8 point 1: nothing to squat or open by name. Every create call passes None as the name."""
    tree = ast.parse(Path(helper_launch.__file__).read_text(encoding="utf-8"))
    creates = {"CreateFileMappingW", "CreateEventW", "CreateJobObjectW"}
    seen: dict[str, int] = dict.fromkeys(creates, 0)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in creates
        ):
            last = node.args[-1]
            assert isinstance(last, ast.Constant), ast.dump(node)
            assert last.value is None, ast.dump(node)
            seen[node.func.attr] += 1
    assert all(seen.values()), seen


# ---- the start/close sequence over a fake ------------------------------------------------------


@dataclass
class FakeWin32:
    """Records every call; on process creation, plays a helper that does `behaviour`."""

    behaviour: str = "ready"
    raise_at: str | None = None
    machine: tuple[int, int] = (UNKNOWN, AMD64)
    job_list_fails: bool = False
    assign_fails: bool = False
    calls: list[tuple[object, ...]] = field(default_factory=list)
    buffers: dict[int, bytearray] = field(default_factory=dict)
    alias: dict[int, int] = field(default_factory=dict)
    inheritable: set[int] = field(default_factory=set)
    open_handles: set[int] = field(default_factory=set)
    next_handle: int = 100
    pid: int = 777
    exited: bool = False

    def _maybe_raise(self, step: str) -> None:
        if self.raise_at == step:
            raise RuntimeError(f"an unexpected failure in {step}")

    def _new(self) -> int:
        self.next_handle += 4
        self.open_handles.add(self.next_handle)
        return self.next_handle

    def _buf(self, handle: int) -> bytearray:
        return self.buffers[self.alias.get(handle, handle)]

    def process_machine(self) -> tuple[int, int]:
        return self.machine

    def user_security(self) -> object:
        self._maybe_raise("user_security")
        self.calls.append(("security",))
        return object()

    def create_job(self, sa: object) -> int:
        self._maybe_raise("create_job")
        del sa
        h = self._new()
        self.calls.append(("job", h))
        return h

    def limit_job(self, job: int) -> None:
        self._maybe_raise("limit_job")
        self.calls.append(("limit", job))

    def create_section(self, sa: object, size: int) -> int:
        self._maybe_raise("create_section")
        del sa
        h = self._new()
        self.buffers[h] = bytearray(size)
        self.calls.append(("section", h, size))
        return h

    def map_view(self, section: int, size: int, *, writable: bool) -> memoryview:
        self._maybe_raise("map_view")
        view = memoryview(self._buf(section))
        assert view.nbytes == size
        return view if writable else view.toreadonly()

    def unmap(self, view: memoryview) -> None:
        del view
        self.calls.append(("unmap",))

    def create_event(self, sa: object, *, manual_reset: bool) -> int:
        self._maybe_raise("create_event")
        del sa
        h = self._new()
        self.calls.append(("event", h, manual_reset))
        return h

    def duplicate(self, handle: int, access: int) -> int:
        self._maybe_raise("duplicate")
        h = self._new()
        if handle in self.buffers or handle in self.alias:
            self.alias[h] = self.alias.get(handle, handle)
        self.calls.append(("dup", handle, access, h))
        return h

    def duplicate_self(self, access: int) -> int:
        self._maybe_raise("duplicate_self")
        h = self._new()
        self.calls.append(("dup_self", access, h))
        return h

    def set_inherit(self, handle: int, on: bool) -> None:
        self._maybe_raise("set_inherit")
        (self.inheritable.add if on else self.inheritable.discard)(handle)

    def create_process(
        self,
        app: str,
        *,
        cmdline: str,
        env: str,
        flags: int,
        inherit: tuple[int, ...],
        jobs: tuple[int, ...],
    ) -> tuple[int, int, int]:
        self._maybe_raise("create_process")
        assert set(inherit) <= self.inheritable, "an inherited handle was not inheritable"
        self.calls.append(("create", app, cmdline, env, flags, inherit, jobs))
        if jobs and self.job_list_fails:
            raise OSError(87, "JOB_LIST refused")
        process, thread = self._new(), self._new()
        if not flags & CREATE_SUSPENDED:
            self._play(cmdline)
        else:
            self._pending = cmdline
        return process, thread, self.pid

    def assign(self, job: int, process: int) -> None:
        self.calls.append(("assign", job, process))
        if self.assign_fails:
            raise OSError(5, "denied")

    def resume(self, thread: int) -> None:
        self.calls.append(("resume", thread))
        self._play(self._pending)

    def _play(self, cmdline: str) -> None:
        handles, _version = parse_helper_argv(cmdline.split()[-6:])
        config = config_words(memoryview(self._buf(handles.config)))
        status = status_words(memoryview(self._buf(handles.status)))
        assert config_error(config) == 0
        if self.behaviour == "ready":
            status.set("pid", self.pid)
            status.set("nonce_echo_lo", config.get("nonce_lo"))
            status.set("nonce_echo_hi", config.get("nonce_hi"))
            status.set("state", STATE_READY)
        elif self.behaviour.startswith("fail:"):
            status.set("error", int(self.behaviour[5:]))
            status.set("state", STATE_FAILED)
        elif self.behaviour == "exit":
            self.exited = True
        elif self.behaviour == "wrong_pid":
            status.set("pid", self.pid + 1)
            status.set("state", STATE_READY)

    def reset_event(self, handle: int) -> None:
        self._maybe_raise("reset_event")
        self.calls.append(("reset", handle))

    def set_event(self, handle: int) -> None:
        self.calls.append(("set", handle))
        self.exited = True  # the helper quits when told to

    def wait_any(self, handles: tuple[int, ...], timeout_ms: int) -> int | None:
        self.calls.append(("wait", handles, timeout_ms))
        return 1 if self.exited else (None if self.behaviour == "hang" else 0)

    def process_id(self, process: int) -> int:
        self._maybe_raise("process_id")
        del process
        return self.pid

    def has_exited(self, process: int) -> bool:
        del process
        return self.exited

    def exit_code(self, process: int) -> int | None:
        del process
        return 3 if self.exited else None

    def terminate_job(self, job: int) -> None:
        self.calls.append(("terminate_job", job))
        self.exited = True

    def terminate_process(self, process: int) -> None:
        self.calls.append(("terminate_process", process))
        self.exited = True

    def close(self, handle: int) -> None:
        self.open_handles.discard(handle)
        self.calls.append(("close", handle))

    def named(self, name: str) -> list[tuple[object, ...]]:
        return [c for c in self.calls if c[0] == name]


def _helper(w: FakeWin32, **kw: object) -> HelperProcess:
    options: dict[str, object] = {
        "interpreter": Path(r"C:\py\python.exe"),
        "launcher": Path(r"D:\v\python.exe"),
        "system_root": r"C:\Windows",
        "run_tag": 0x1234_5678,
        "probe_vk": 0x0E,
        "kill_mods": 0x7,
        "kill_vk": 0x53,
        "hook_timeout_ms": 1000,
        "pointer_size": 8,
        **kw,
    }
    return HelperProcess(w, **options)  # type: ignore[arg-type]  # the fake stands in for Win32


def test_a_start_creates_the_helper_inside_the_job_with_exactly_five_inherited_handles() -> None:
    w = FakeWin32()
    helper = _helper(w)
    helper.start()
    (create,) = w.named("create")
    _, app, cmdline, env, flags, inherit, jobs = create
    (job,) = (c[1] for c in w.named("job"))
    assert app == r"C:\py\python.exe"
    assert jobs == (job,)
    assert len(inherit) == 5  # type: ignore[arg-type]  # a recorded tuple
    assert job not in inherit  # type: ignore[operator]  # a recorded tuple
    handles, _v = parse_helper_argv(str(cmdline).split()[-6:])
    assert handles.all() == inherit
    assert flags == creation_flags(suspended=False)
    assert env == environment_block(system_root=r"C:\Windows", launcher=Path(r"D:\v\python.exe"))
    assert not w.inheritable  # inheritable only around the creation


def test_the_helper_gets_a_read_only_config_and_a_terminate_capable_parent_handle() -> None:
    w = FakeWin32()
    _helper(w).start()
    (create,) = w.named("create")
    handles: HelperHandles = parse_helper_argv(str(create[2]).split()[-6:])[0]
    dups = {c[3]: c[2] for c in w.named("dup")}
    assert dups[handles.config] == FILE_MAP_READ
    (dup_self,) = w.named("dup_self")
    assert dup_self[1:] == (SYNCHRONIZE | PROCESS_TERMINATE, handles.parent)


def test_steers_status_view_is_read_only_and_its_config_view_writable() -> None:
    w = FakeWin32()
    helper = _helper(w)
    helper.start()
    assert helper.status.get("state") == STATE_READY
    with pytest.raises(TypeError):
        helper.status.set("kill", 0)
    helper.config.set("ask_depth", 1)
    assert config_error(helper.config) == 0  # ask_depth is outside the CRC


def test_without_job_list_the_helper_is_created_suspended_then_assigned_then_resumed() -> None:
    """P6 (rev 2): the fallback never lets the helper run before it is in the job."""
    w = FakeWin32(job_list_fails=True)
    _helper(w).start()
    kinds = [c[0] for c in w.calls if c[0] in {"create", "assign", "resume"}]
    assert kinds == ["create", "create", "assign", "resume"]
    second = w.named("create")[1]
    assert second[4] == creation_flags(suspended=True)
    assert second[6] == ()


def test_a_failed_assignment_terminates_the_suspended_helper_and_refuses() -> None:
    w = FakeWin32(job_list_fails=True, assign_fails=True)
    with pytest.raises(SteerError, match="monitor_unavailable"):
        _helper(w).start()
    assert w.named("terminate_process")
    assert not w.named("resume")


@pytest.mark.parametrize(
    ("behaviour", "detail"),
    [
        (f"fail:{ERROR_HOTKEY}", "kill_key_unavailable"),
        (f"fail:{ERROR_HOOKS}", "monitor_unavailable"),
        ("exit", "monitor_unavailable"),
        ("hang", "monitor_unavailable"),
        ("wrong_pid", "monitor_unavailable"),
    ],
)
def test_a_helper_that_does_not_become_ready_is_a_refusal_and_is_ended(
    behaviour: str, detail: str
) -> None:
    """K16: if the helper fails to start, v2 refuses to start."""
    w = FakeWin32(behaviour=behaviour)
    with pytest.raises(SteerError, match=detail):
        _helper(w).start()
    assert w.named("terminate_job")
    assert w.open_handles == set()  # everything Steer opened is closed again


def test_a_non_x64_machine_is_refused_before_anything_is_created() -> None:
    w = FakeWin32(machine=(UNKNOWN, ARM64))
    with pytest.raises(SteerError, match="monitor_unavailable"):
        _helper(w).start()
    assert not w.named("job")


def test_close_asks_the_helper_to_quit_then_closes_the_job_and_is_idempotent() -> None:
    w = FakeWin32()
    helper = _helper(w)
    helper.start()
    helper.close()
    helper.close()
    assert len(w.named("set")) == 1
    assert not w.named("terminate_job")  # it quit on its own
    assert w.open_handles == set()


def test_a_helper_that_will_not_quit_is_terminated_by_its_job() -> None:
    w = FakeWin32()
    helper = _helper(w)
    helper.start()
    w.set_event = lambda handle: w.calls.append(("set", handle))  # type: ignore[method-assign]  # ignores quit
    helper.close()
    assert w.named("terminate_job")
    assert w.open_handles == set()


def test_the_config_holds_the_runs_values_and_a_fresh_nonce() -> None:
    w1, w2 = FakeWin32(), FakeWin32()
    h1, h2 = _helper(w1), _helper(w2)
    h1.start()
    h2.start()
    c = h1.config
    assert (c.get("run_tag"), c.get("probe_vk"), c.get("kill_mods"), c.get("kill_vk")) == (
        0x1234_5678, 0x0E, 0x7, 0x53,
    )  # fmt: skip
    assert c.get("hook_timeout_ms") == 1000
    assert (c.get("nonce_lo"), c.get("nonce_hi")) != (h2.config.get("nonce_lo"),
                                                       h2.config.get("nonce_hi"))  # fmt: skip


# ---- #375 review P2: any failure at any start step is a refusal (K16) -----------------------


@pytest.mark.parametrize(
    "step",
    [
        "user_security",
        "create_job",
        "limit_job",
        "create_section",
        "map_view",
        "create_event",
        "duplicate",
        "duplicate_self",
        "set_inherit",
        "create_process",
        "reset_event",
        "process_id",
    ],
)
def test_a_non_os_error_at_any_win32_step_is_a_refusal_with_nothing_left(step: str) -> None:
    w = FakeWin32(raise_at=step)
    with pytest.raises(SteerError, match="monitor_unavailable"):
        _helper(w).start()
    assert w.open_handles == set()
    if w.named("create"):
        assert w.named("terminate_job") or w.named("terminate_process")


# ---- #375 review P3, P4: close always releases, and nothing reads unmapped memory -------------


def test_close_releases_everything_even_when_its_wait_fails() -> None:
    w = FakeWin32()
    helper = _helper(w)
    helper.start()

    def broken_wait(handles: tuple[int, ...], timeout_ms: int) -> int | None:
        raise OSError(6, "the handle is invalid")

    w.wait_any = broken_wait  # type: ignore[method-assign]  # a wait that fails
    with pytest.raises(OSError, match="invalid"):
        helper.close()
    assert w.open_handles == set()


def test_after_close_the_words_cannot_be_read() -> None:
    """P4: `Words` over unmapped memory would be an access violation."""
    w = FakeWin32()
    helper = _helper(w)
    helper.start()
    helper.close()
    with pytest.raises(RuntimeError):
        _ = helper.status
    with pytest.raises(RuntimeError):
        _ = helper.config
