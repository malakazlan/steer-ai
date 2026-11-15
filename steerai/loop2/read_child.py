"""A pure-read v2 child, for diagnosis only (not a user flag; probe-marked tests only): the real
child's setup, then reads and nothing else.

Why: in the 3d shakedown of 2026-11-15 (from #380), the first child saw the page and every later
child read blank for the whole first-ready wait, with the bound window in front and uncovered.
This child repeats the real setup, in the harness's own order, without a task: the session mutex,
the hook monitor and the input tag (`LiveWorld`), `bring_to_front`, the ports' traced first
observation (`first_facts`), then one observe every `READ_POLL_S` until `READ_S` from the start.
It never decides and never acts; its model calls refuse if anything tries one.

Two arms ([V2.57a] comparability, and a candidate for the blank pages): `mta`, the real loop
thread (it enters the MTA, as the v2 CLI does), and `sta`, the same path on a thread in a
single-threaded apartment, as v1's client has always run. LiveWorld refuses anything but the MTA,
so for the `sta` arm only, the platform reports the STA thread as MTA to that guard: a
diagnostic arm, never a task.

Child: `python -m steerai.loop2.read_child child --hwnd H --arm mta|sta --out reads.jsonl
--trace trace.jsonl`. Sequence (the harness's order: the 3d positive controls, their Chrome's
exit, then one child per 3d variant, each on a fresh page in a throwaway Chrome):
`python -m steerai.loop2.read_child sequence --arms mta,sta --out-dir D`.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final, Literal, NoReturn, get_args

from steerai.loop2.first_facts import class_token
from steerai.world.effects import Observed
from steerai.world.observe2 import document_nodes

Arm = Literal["mta", "sta"]
ARMS: Final[tuple[Arm, ...]] = get_args(Arm)
READ_S: Final = 15.0
READ_POLL_S: Final = 0.25
CHILD_CAP_S: Final = 90.0
"""The child's cap: setup, the first-ready wait (10 s) and `READ_S`, with room to spare."""
COINIT_APARTMENTTHREADED: Final = 0x2


def read_shape(seen: Observed, t: float) -> dict[str, object]:
    """One read as counts only: when, how many Documents, the nodes under the top Document
    (None: no single top Document), all nodes, and whether the read was stable."""
    snapshot = seen.snapshot
    inside = document_nodes(snapshot)
    return {
        "t": round(t, 3),
        "documents": sum(e.role == "Document" for e in snapshot.elements),
        "doc_nodes": None if inside is None else len(inside),
        "nodes": len(snapshot.elements),
        "stable": snapshot.stable,
    }


def first_page_s(reads: Sequence[Mapping[str, Any]]) -> float | None:  # Any: JSON
    """The first read's time with something under the top Document; None when none had."""
    for read in reads:
        inside = read.get("doc_nodes")
        if isinstance(inside, int) and inside > 0:
            return float(read["t"])
    return None


def parse_arms(text: str) -> tuple[Arm, ...]:
    """`mta,sta` in the order given; anything else is a usage error."""
    arms = tuple(part.strip() for part in text.split(",") if part.strip())
    if not arms or any(arm not in ARMS for arm in arms):
        raise ValueError(f"arms must be a comma list of {', '.join(ARMS)}")
    return arms  # type: ignore[return-value]  # each checked against ARMS above


WATCHED: Final = frozenset({"chrome.exe", "python.exe", "pythonw.exe"})
"""The process names the leftover list reports: Steer's own Python and its Chromes, nothing else."""
Mode = Literal["mixed", "real"]
Then = Literal["none", "input", "model"]
THENS: Final[tuple[Then, ...]] = get_args(Then)
Exp = Literal["access", "fade30", "fade120", "input", "model"]
EXPS: Final[tuple[Exp, ...]] = get_args(Exp)
FORCE_A11Y: Final = "--force-renderer-accessibility"
"""Diagnostic only, never in a scored run: does Chrome expose the page when told to."""
MODES: Final[tuple[Mode, ...]] = get_args(Mode)


def leftovers(table: Sequence[tuple[int, int, str]], ours: set[int]) -> list[dict[str, object]]:
    """The watched processes this run launched (`ours`, every pid it launched or saw under one)
    or whose parent it launched, still alive in `table` ((pid, parent pid, image name)); never
    another process's name."""
    return [
        {"name": name, "pid": pid, "ppid": ppid}
        for pid, ppid, name in table
        if name.lower() in WATCHED and (pid in ours or ppid in ours)
    ]


def _never_called(_system: list[str], _blocks: list[str]) -> NoReturn:
    raise RuntimeError("read_only: this child never calls the model")


# ---- the child (live) ---------------------------------------------------------------------------


def run_child_reads(  # pragma: no cover - live
    hwnd: int, *, arm: Arm, out: Path, trace: Path, then: Then = "none"
) -> int:
    """The real child's setup on `hwnd`, then reads, in `arm`; one JSON line per read in `out`,
    then a summary line. `then`, after the first observation: `input` sends a focus, a type and
    an Enter to the page's first field through the real act port (no model); `model` makes two
    real model calls (no input). Live, Windows only."""
    from steerai.loop2.decide_port import TaskGenerations  # noqa: PLC0415 - live only
    from steerai.loop2.decision import ActionDoc  # noqa: PLC0415
    from steerai.loop2.live_world import (  # noqa: PLC0415
        LiveConfig,
        LiveWorld,
        WindowsPlatform,
        _ole32,
        run_on_loop_thread,
    )
    from steerai.loop2.loop import Dispatch, PortInterrupted, WindowCovered  # noqa: PLC0415
    from steerai.loop2.task import Budgets, Envelope, Task  # noqa: PLC0415
    from steerai.orchestrator.trace import JsonLinesTrace  # noqa: PLC0415
    from steerai.probes.windows import process_name, window_pid  # noqa: PLC0415

    class ReadOnlyPlatform(WindowsPlatform):
        def uia(self) -> tuple[Any, Any]:  # Any: the UiaClient and its bindings
            if arm == "mta":
                return super().uia()
            ole32 = _ole32()
            if int(ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)) & 0xFFFFFFFF not in (0, 1):
                raise OSError("not an STA")
            self._entered = True
            if "comtypes" not in sys.modules:
                sys.coinit_flags = COINIT_APARTMENTTHREADED  # type: ignore[attr-defined]  # comtypes reads it
            from steerai.action.execution import UiaBindings  # noqa: PLC0415
            from steerai.probes.uia_snapshot import UiaClient  # noqa: PLC0415

            return UiaClient(), UiaBindings.load()

        def apartment(self) -> str:
            real = super().apartment()
            # the sta arm only: LiveWorld's MTA guard is told MTA so the same path runs (above)
            return "MTA" if arm == "sta" and real in ("STA", "MAINSTA") else real

        def provider(self) -> Any:  # Any: a never-called `Call`
            return _never_called

        def counter(self) -> Any:  # Any: a never-called `Counter`
            return _never_called

    pid = window_pid(hwnd)
    config = LiveConfig(
        task_id="read-child",
        hwnd=hwnd,
        kill_key="ctrl+alt+k",
        trace_level="redacted",
        trace=JsonLinesTrace(trace),
        generations=TaskGenerations(),
        clock=time.monotonic,
        pause=time.sleep,
        bound_pid=pid,
        bound_image=process_name(pid).lower(),
    )
    task = Task(
        id="read-child",
        goal="read only",
        subgoals=(),
        envelope=Envelope(grants=(), mode="bootstrap"),
        budgets=Budgets(
            max_actions=1, max_model_calls=1, max_tokens=1, max_cost_usd=0.01, max_wall_s=60.0
        ),
        history=[],
    )
    platform = ReadOnlyPlatform()

    def work() -> list[dict[str, object]]:
        reads: list[dict[str, object]] = []
        with LiveWorld(config, platform) as world:
            started = time.monotonic()
            world.bring_to_front()
            ports = world.ports(task)
            first = ports.observe()
            reads.append(read_shape(first, time.monotonic() - started))
            if then == "input":
                reads.append({"acts": _input_acts(ports, first, ActionDoc, Dispatch)})
            elif then == "model":
                call = WindowsPlatform.provider(platform)
                for _ in range(2):
                    reply = call(["Answer with one word."], ["Say ok."])
                    reads.append({"model_call": type(reply).__name__})
            while (now := time.monotonic() - started) < READ_S:
                try:
                    reads.append(read_shape(ports.observe(), now))
                except (WindowCovered, PortInterrupted) as stopped:
                    reads.append({"t": round(now, 3), "stopped": type(stopped).__name__})
                    break
                time.sleep(READ_POLL_S)
            del ports  # nothing holding COM outlives the world on this thread
        return reads

    reads = run_on_loop_thread(work, release=platform.release_com)  # each arm: a fresh thread
    with out.open("w", encoding="utf-8") as handle:
        for read in reads:
            handle.write(json.dumps(read) + "\n")
        handle.write(json.dumps({"summary": {"arm": arm, "then": then,
                                             "first_page_s": first_page_s(reads),
                                             "reads": len(reads)}}) + "\n")  # fmt: skip
    return 0


def _input_acts(
    ports: Any, seen: Observed, action_doc: Any, dispatch: Any
) -> list[str]:  # pragma: no cover
    """A focus, a type and an Enter on the page's first field through the real act port, each
    resolved on a fresh observation; their deliveries as closed tokens. Any: the live ports and
    the two classes, imported live."""
    fields = [e for e in seen.snapshot.elements if e.role == "Edit" and e.parent_id is not None]
    page = [e for e in fields if e.name != "Address and search bar"]
    if not page:
        return ["no_field"]
    field = page[0]
    plan = [
        (action_doc(verb="focus", target=field.id, subgoal="s1"), "R0", None, None),
        (action_doc(verb="type", target=field.id, subgoal="s1", text="red shoes"), "R0",
         "goal_span", "red shoes"),
        (action_doc(verb="press", subgoal="s1", key="Enter"), "R1", None, None),
    ]  # fmt: skip
    done: list[str] = []
    for action, risk, provenance, text in plan:
        ports.observe()  # the act port resolves on the last observation handed out
        sent = dispatch(
            action=action,
            runtime_id=field.runtime_id,
            role=field.role,
            name=field.name,
            risk=risk,
            provenance=provenance,
            text=text,
        )
        result = ports.act(sent)
        done.append(f"{action.verb}:{result.action.delivery.value}")
    return done


def run_plain(hwnd: int, *, out: Path) -> int:  # pragma: no cover - live, Windows only
    """A plain UIA reader: no session, no hooks, no LiveWorld; v2's world observe on an MTA
    thread every `READ_POLL_S` for `READ_S`."""
    import ctypes  # noqa: PLC0415 - Windows only
    import threading  # noqa: PLC0415

    import comtypes.client  # noqa: F401, PLC0415 - imported here so the reader can enter the MTA

    found: list[list[dict[str, object]]] = []

    def body() -> None:
        ctypes.windll.ole32.CoInitializeEx(None, 0)
        try:
            from steerai.probes.uia_snapshot import UiaClient  # noqa: PLC0415
            from steerai.world.effects import poll_tier  # noqa: PLC0415
            from steerai.world.model import WorldModel  # noqa: PLC0415

            model = WorldModel(UiaClient())
            reads: list[dict[str, object]] = []
            started = time.monotonic()
            while (now := time.monotonic() - started) < READ_S:
                reads.append(read_shape(poll_tier(model.observe(hwnd)), now))
                time.sleep(READ_POLL_S)
            found.append(reads)
        finally:
            ctypes.windll.ole32.CoUninitialize()

    thread = threading.Thread(target=body)
    thread.start()
    thread.join()
    reads = found[0] if found else []
    with out.open("w", encoding="utf-8") as handle:
        for read in reads:
            handle.write(json.dumps(read) + "\n")
        handle.write(json.dumps({"summary": {"plain": True, "first_page_s": first_page_s(reads),
                                             "reads": len(reads)}}) + "\n")  # fmt: skip
    return 0


# ---- the sequence (live) ------------------------------------------------------------------------


def run_sequence(arms: Sequence[Arm], out_dir: Path) -> int:  # pragma: no cover - live
    """For each arm in turn: the 3d positive controls and their Chrome's exit, as the harness
    runs them, then one read child per 3d variant on a fresh page in a throwaway Chrome. One
    JSON line per child on stdout; only this run's own Chromes are closed, by PID."""
    import shutil  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    from steerai.loop2.stage1 import (  # noqa: PLC0415
        _idle_s,
        precheck,
        wait_for_exit,
        wait_until_ready,
    )
    from steerai.probes import instant_search as fixture  # noqa: PLC0415 - live only
    from steerai.probes.chrome_a11y import (  # noqa: PLC0415
        CHROME_DEFAULT,
        launch_chrome,
        stop_chrome,
    )
    from steerai.probes.layer_b import _ours  # noqa: PLC0415
    from steerai.probes.layer_b_pages import PageServer  # noqa: PLC0415
    from steerai.probes.memory_guard import free_physical_mb  # noqa: PLC0415
    from steerai.probes.process_tree import process_table, tree_pids  # noqa: PLC0415
    from steerai.probes.windows import wait_window_for_pid  # noqa: PLC0415

    def emit(row: Mapping[str, object]) -> None:
        sys.stdout.write(json.dumps(dict(row)) + "\n")
        sys.stdout.flush()

    for arm in arms:
        ours: set[int] = set()

        def record(pid: int, ours: set[int] = ours) -> None:
            ours.update(tree_pids(pid, process_table()) or {pid})

        passed = sum(
            fixture.passes(v, fixture.run_control(v, on_stop=record)) for v in fixture.VARIANTS
        )
        left = wait_for_exit(ours, alive=lambda: {p for p, _ in process_table()},
                             sleep=time.sleep, clock=time.monotonic)  # fmt: skip
        emit({"arm": arm, "controls": passed, "controls_left": len(left)})
        for variant in fixture.VARIANTS:
            refused = wait_until_ready(
                lambda: precheck(free_mb=free_physical_mb, idle_s=_idle_s),
                sleep=time.sleep, clock=time.monotonic,
            )  # fmt: skip
            if refused is not None:
                emit({"arm": arm, "variant": variant, "not_started": refused})
                continue
            run_dir = out_dir / f"{arm}-{variant}"
            run_dir.mkdir(parents=True, exist_ok=True)
            tmp = Path(tempfile.mkdtemp(prefix="steer-readchild-"))
            with PageServer(fixture.pages()) as server:
                port = fixture._free_port()
                proc = launch_chrome(
                    CHROME_DEFAULT, tmp / "profile", port,
                    f"http://127.0.0.1:{server.port}/{variant}",
                )  # fmt: skip
                try:
                    window = wait_window_for_pid(proc.pid, timeout_s=30.0, process=proc)
                    cdp, _ = _ours(port, server.port)
                    try:
                        fixture._wait_ready(cdp)
                        argv = [
                            sys.executable, "-m", "steerai.loop2.read_child", "child",
                            "--hwnd", str(window.hwnd), "--arm", arm,
                            "--out", str(run_dir / "reads.jsonl"),
                            "--trace", str(run_dir / "trace.jsonl"),
                        ]  # fmt: skip
                        import subprocess  # noqa: PLC0415

                        done = subprocess.run(  # noqa: S603 - our own module, a fixed argv
                            argv, capture_output=True, timeout=CHILD_CAP_S, check=False
                        )
                    finally:
                        cdp.close()
                    emit({"arm": arm, "variant": variant, "code": done.returncode,
                          **_summary(run_dir / "reads.jsonl")})  # fmt: skip
                finally:
                    stop_chrome(proc, tmp / "profile")
                    shutil.rmtree(tmp, ignore_errors=True)
    return 0


def _named_table() -> list[tuple[int, int, str]]:  # pragma: no cover - live, Windows only
    """Every (pid, parent pid, image name) now, from one Toolhelp32 snapshot."""
    import ctypes  # noqa: PLC0415 - Windows only
    from ctypes import wintypes  # noqa: PLC0415

    from steerai.probes.process_tree import TH32CS_SNAPPROCESS, _ProcessEntry  # noqa: PLC0415

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot is None or snapshot == wintypes.HANDLE(-1).value:
        raise OSError("CreateToolhelp32Snapshot failed")
    table: list[tuple[int, int, str]] = []
    try:
        entry = _ProcessEntry()
        entry.dwSize = ctypes.sizeof(_ProcessEntry)
        more = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            table.append(
                (int(entry.th32ProcessID), int(entry.th32ParentProcessID), str(entry.szExeFile))
            )
            more = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    return table


def _first_record(trace: Path) -> dict[str, object] | None:  # pragma: no cover - live
    try:
        lines = trace.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        record = json.loads(line) if line.strip() else {}
        if record.get("event") == "first_observation":
            return dict(record)
    return None


def run_acting(mode: Mode, out_dir: Path, price: Path) -> int:  # noqa: PLR0915  # pragma: no cover
    """The 3d controls and their Chrome's exit, then one child per 3d variant: in `mixed`, the
    real v2 CLI on the first variant (acting, as the harness runs it) and pure-read children on
    the rest; in `real`, the real CLI on every variant. Before each child, the processes this run
    launched that are still alive (`leftovers`); after it, its `first_observation` record."""
    import shutil  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    from steerai.loop2.stage1 import (  # noqa: PLC0415
        TASK_CAP_S,
        THREE_D,
        _idle_s,
        _start,
        child_argv,
        precheck,
        run_child,
        wait_for_exit,
        wait_until_ready,
    )
    from steerai.probes import instant_search as fixture  # noqa: PLC0415 - live only
    from steerai.probes.chrome_a11y import (  # noqa: PLC0415
        CHROME_DEFAULT,
        launch_chrome,
        stop_chrome,
    )
    from steerai.probes.layer_b import _ours  # noqa: PLC0415
    from steerai.probes.layer_b_pages import PageServer  # noqa: PLC0415
    from steerai.probes.memory_guard import free_physical_mb  # noqa: PLC0415
    from steerai.probes.process_tree import process_table, tree_pids  # noqa: PLC0415
    from steerai.probes.suite_chunks import kill_tree  # noqa: PLC0415
    from steerai.probes.windows import wait_window_for_pid  # noqa: PLC0415

    def emit(row: Mapping[str, object]) -> None:
        sys.stdout.write(json.dumps(dict(row)) + "\n")
        sys.stdout.flush()

    ours: set[int] = set()

    def record(pid: int) -> None:
        ours.update(tree_pids(pid, process_table()) or {pid})

    passed = sum(
        fixture.passes(v, fixture.run_control(v, on_stop=record)) for v in fixture.VARIANTS
    )
    left = wait_for_exit(set(ours), alive=lambda: {p for p, _ in process_table()},
                         sleep=time.sleep, clock=time.monotonic)  # fmt: skip
    emit({"mode": mode, "controls": passed, "controls_left": len(left)})
    for index, task in enumerate(THREE_D):
        variant = task.page.lstrip("/")
        real = mode == "real" or index == 0
        emit({"before": variant, "leftovers": leftovers(_named_table(), ours)})
        refused = wait_until_ready(
            lambda: precheck(free_mb=free_physical_mb, idle_s=_idle_s),
            sleep=time.sleep, clock=time.monotonic,
        )  # fmt: skip
        if refused is not None:
            emit({"variant": variant, "not_started": refused})
            continue
        run_dir = out_dir / f"{mode}-{variant}"
        run_dir.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix="steer-readchild-"))
        with PageServer(fixture.pages()) as server:
            port = fixture._free_port()
            proc = launch_chrome(
                CHROME_DEFAULT, tmp / "profile", port, f"http://127.0.0.1:{server.port}{task.page}"
            )
            ours.add(proc.pid)
            try:
                window = wait_window_for_pid(proc.pid, timeout_s=30.0, process=proc)
                cdp, _ = _ours(port, server.port)
                try:
                    fixture._wait_ready(cdp)
                    trace = run_dir / "trace.jsonl"
                    if real:
                        argv = child_argv(task, hwnd=window.hwnd, price=price, trace=trace)
                        out = run_dir / "final.jsonl"
                    else:
                        argv = [
                            sys.executable, "-m", "steerai.loop2.read_child", "child",
                            "--hwnd", str(window.hwnd), "--arm", "mta",
                            "--out", str(run_dir / "reads.jsonl"), "--trace", str(trace),
                        ]  # fmt: skip
                        out = run_dir / "stdout.txt"

                    def started(command: list[str], target: Path) -> Any:  # Any: a Popen
                        child = _start(command, target)
                        ours.add(child.pid)
                        return child

                    ran = run_child(argv, cap_s=TASK_CAP_S if real else CHILD_CAP_S, out=out,
                                    start=started, kill=kill_tree)  # fmt: skip
                    record(proc.pid)
                finally:
                    cdp.close()
            finally:
                stop_chrome(proc, tmp / "profile")
                shutil.rmtree(tmp, ignore_errors=True)
        row: dict[str, object] = {"variant": variant, "child": "real" if real else "read",
                                  "killed": ran.killed, "code": ran.code,
                                  "first_observation": _first_record(trace)}  # fmt: skip
        if real and ran.final is not None:
            final = ran.final
            row.update({k: final.get(k) for k in ("status", "reason", "used", "rejections")})
        if not real:
            row.update(_summary(run_dir / "reads.jsonl"))
        emit(row)
    emit({"after": "all", "leftovers": leftovers(_named_table(), ours)})
    return 0


def run_follow(exp: Exp, out_dir: Path, price: Path) -> int:  # noqa: PLR0915  # pragma: no cover
    """The follow-ups after the acting-child reproduction (no controls: the trigger is the first
    child). `access`: the real CLI on 3d-a, then a read child on a Chrome with
    `--force-renderer-accessibility`, then one without it. `fade30` / `fade120`: the real CLI on
    3d-a, a wait, then a read child and a plain reader side by side on one fresh Chrome.
    `input` / `model`: a read child that only sends input / only calls the model on 3d-a, then
    read children on b and c-button. Wall-clock times of each child's start and end are kept."""
    import shutil  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    from steerai.loop2.stage1 import THREE_D, _idle_s, child_argv  # noqa: PLC0415
    from steerai.probes import instant_search as fixture  # noqa: PLC0415 - live only
    from steerai.probes.chrome_a11y import (  # noqa: PLC0415
        CHROME_DEFAULT,
        launch_chrome,
        stop_chrome,
    )
    from steerai.probes.layer_b import _ours  # noqa: PLC0415
    from steerai.probes.layer_b_pages import PageServer  # noqa: PLC0415
    from steerai.probes.windows import wait_window_for_pid  # noqa: PLC0415

    def emit(row: Mapping[str, object]) -> None:
        sys.stdout.write(json.dumps(dict(row)) + "\n")
        sys.stdout.flush()

    def on_chrome(variant: str, name: str, children: Any, *, force: bool = False) -> None:
        """One fresh Chrome on `variant`; `children(hwnd, run_dir)` returns the argvs to run on
        it at once (side by side), each with its own label."""
        run_dir = out_dir / name
        run_dir.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix="steer-readchild-"))
        with PageServer(fixture.pages()) as server:
            port = fixture._free_port()
            proc = launch_chrome(
                CHROME_DEFAULT, tmp / "profile", port, f"http://127.0.0.1:{server.port}/{variant}",
                extra_args=(FORCE_A11Y,) if force else (),
            )  # fmt: skip
            try:
                window = wait_window_for_pid(proc.pid, timeout_s=30.0, process=proc)
                cdp, _ = _ours(port, server.port)
                try:
                    fixture._wait_ready(cdp)
                    idle_at_start, saver_at_start = round(_idle_s(), 1), _screensaver()
                    started = time.time()
                    runs = {
                        label: subprocess.Popen(  # noqa: S603 - our own modules, fixed argvs
                            argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                        )
                        for label, argv in children(window.hwnd, run_dir).items()
                    }
                    codes = {label: run.wait(timeout=CHILD_CAP_S) for label, run in runs.items()}
                    ended = time.time()
                finally:
                    cdp.close()
            finally:
                stop_chrome(proc, tmp / "profile")
                shutil.rmtree(tmp, ignore_errors=True)
        row: dict[str, object] = {"name": name, "variant": variant, "force_a11y": force,
                                  "idle_s_at_start": idle_at_start,
                                  "screensaver_at_start": saver_at_start,
                                  "start_wall": round(started, 3), "end_wall": round(ended, 3),
                                  "codes": codes}  # fmt: skip
        for label in codes:
            row[label] = {
                "first_observation": _first_record(run_dir / f"{label}-trace.jsonl"),
                **_summary(run_dir / f"{label}-reads.jsonl"),
            }
        final = run_dir / "real-final.jsonl"
        if final.exists():
            lines = [ln for ln in final.read_text(encoding="utf-8").splitlines() if ln.strip()]
            if lines:
                last = json.loads(lines[-1])
                row["real"] = {k: last.get(k) for k in ("status", "reason", "used", "rejections")}
        emit(row)

    def read(then: Then = "none") -> Any:
        def argvs(hwnd: int, run_dir: Path) -> dict[str, list[str]]:
            return {"read": [
                sys.executable, "-m", "steerai.loop2.read_child", "child", "--hwnd", str(hwnd),
                "--arm", "mta", "--then", then,
                "--out", str(run_dir / "read-reads.jsonl"),
                "--trace", str(run_dir / "read-trace.jsonl"),
            ]}  # fmt: skip

        return argvs

    def real(hwnd: int, run_dir: Path) -> dict[str, list[str]]:
        argv = child_argv(THREE_D[0], hwnd=hwnd, price=price, trace=run_dir / "real-trace.jsonl")
        # the CLI writes its final line on stdout: through a shell-free redirect of our own
        return {"real": [sys.executable, "-c", _TEE, str(run_dir / "real-final.jsonl"), *argv]}

    def side_by_side(hwnd: int, run_dir: Path) -> dict[str, list[str]]:
        return {
            **read()(hwnd, run_dir),
            "plain": [
                sys.executable, "-m", "steerai.loop2.read_child", "plain", "--hwnd", str(hwnd),
                "--out", str(run_dir / "plain-reads.jsonl"),
            ],
        }  # fmt: skip

    if exp == "access":
        on_chrome("a", "access-1-real", real)
        on_chrome("b", "access-2-read-forced", read(), force=True)
        on_chrome("c-button", "access-3-read-plain", read())
    elif exp in ("fade30", "fade120"):
        on_chrome("a", f"{exp}-1-real", real)
        time.sleep(30.0 if exp == "fade30" else 120.0)
        on_chrome("b", f"{exp}-2-side-by-side", side_by_side)
    else:
        only: Then = "input" if exp == "input" else "model"
        on_chrome("a", f"{exp}-1-{exp}-only", read(only))
        on_chrome("b", f"{exp}-2-read", read())
        on_chrome("c-button", f"{exp}-3-read", read())
    return 0


def _screensaver() -> bool | None:  # pragma: no cover - live, Windows only
    """SPI_GETSCREENSAVERRUNNING (0x0072); None when unread."""
    import ctypes  # noqa: PLC0415 - Windows only

    running = ctypes.c_int(0)
    if not ctypes.windll.user32.SystemParametersInfoW(0x0072, 0, ctypes.byref(running), 0):
        return None
    return bool(running.value)


CELLS: Final = (
    ("runner", "runner"),
    ("fresh", "runner"),
    ("runner", "fresh"),
    ("fresh", "fresh"),
)
"""The 2x2 after one input-only child: who launches the Chrome (L) and who reads it (R)."""
_LAUNCH: Final = (
    "import sys; from pathlib import Path; "
    "from steerai.probes.chrome_a11y import CHROME_DEFAULT, launch_chrome; "
    "proc = launch_chrome(CHROME_DEFAULT, Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3]); "
    "print(proc.pid)"
)
"""A fresh helper process that launches the Chrome and exits at once (Chrome keeps running)."""


class _Launched:  # pragma: no cover - live
    """A Chrome launched by a helper that has exited: `stop_chrome` needs a pid and `wait`."""

    def __init__(self, pid: int) -> None:
        self.pid = pid

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return 0

    def poll(self) -> int | None:
        return None


def run_matrix(out_dir: Path) -> int:  # noqa: PLR0915  # pragma: no cover - live
    """One input-only child on 3d-a, then the 2x2 on fresh Chromes on 3d-b: launched by this
    runner or by a fresh helper process (L), read by this runner in-process or by a plain reader
    in a fresh process (R). Each Chrome's own view of its visibility is read over its DevTools
    port (read-only). Only this run's Chromes are closed, by PID."""
    import shutil  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    from steerai.probes import instant_search as fixture  # noqa: PLC0415 - live only
    from steerai.probes.chrome_a11y import (  # noqa: PLC0415
        CHROME_DEFAULT,
        launch_chrome,
        stop_chrome,
    )
    from steerai.probes.layer_b import _ours  # noqa: PLC0415
    from steerai.probes.layer_b_pages import PageServer  # noqa: PLC0415
    from steerai.probes.windows import wait_window_for_pid  # noqa: PLC0415

    def emit(row: Mapping[str, object]) -> None:
        sys.stdout.write(json.dumps(dict(row)) + "\n")
        sys.stdout.flush()

    def visibility(cdp: Any) -> dict[str, object]:  # Any: a CdpSession
        def value(expression: str) -> object:
            reply = cdp.call("Runtime.evaluate", {"expression": expression, "returnByValue": True})
            return reply.get("result", {}).get("value")

        metrics = cdp.call("Page.getLayoutMetrics", {})
        viewport = metrics.get("cssVisualViewport") or metrics.get("visualViewport") or {}
        return {
            "visibilityState": value("document.visibilityState"),
            "hidden": value("document.hidden"),
            "hasFocus": value("document.hasFocus()"),
            "viewport": {k: viewport.get(k) for k in ("clientWidth", "clientHeight")},
        }

    def cell(name: str, variant: str, launcher: str, reader: str, then: Then = "none") -> None:
        run_dir = out_dir / name
        run_dir.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix="steer-readchild-"))
        with PageServer(fixture.pages()) as server:
            port = fixture._free_port()
            url = f"http://127.0.0.1:{server.port}/{variant}"
            if launcher == "runner":
                proc: Any = launch_chrome(CHROME_DEFAULT, tmp / "profile", port, url)
            else:
                helper = subprocess.run(  # noqa: S603 - our own launcher, a fixed argv
                    [sys.executable, "-c", _LAUNCH, str(tmp / "profile"), str(port), url],
                    capture_output=True, text=True, timeout=60, check=True,
                )  # fmt: skip
                proc = _Launched(int(helper.stdout.strip().splitlines()[-1]))
            try:
                window = wait_window_for_pid(proc.pid, timeout_s=30.0)
                cdp, _ = _ours(port, server.port)
                try:
                    fixture._wait_ready(cdp)
                    started = time.time()
                    reads_path = run_dir / "reads.jsonl"
                    if then != "none":
                        subprocess.run(  # noqa: S603 - our own module, a fixed argv
                            [sys.executable, "-m", "steerai.loop2.read_child", "child",
                             "--hwnd", str(window.hwnd), "--arm", "mta", "--then", then,
                             "--out", str(reads_path), "--trace", str(run_dir / "trace.jsonl")],
                            capture_output=True, timeout=CHILD_CAP_S, check=False,
                        )  # fmt: skip
                    elif reader == "runner":
                        run_plain(window.hwnd, out=reads_path)
                    else:
                        subprocess.run(  # noqa: S603 - our own module, a fixed argv
                            [sys.executable, "-m", "steerai.loop2.read_child", "plain",
                             "--hwnd", str(window.hwnd), "--out", str(reads_path)],
                            capture_output=True, timeout=CHILD_CAP_S, check=False,
                        )  # fmt: skip
                    seen = visibility(cdp)
                finally:
                    cdp.close()
            finally:
                stop_chrome(proc, tmp / "profile")
                shutil.rmtree(tmp, ignore_errors=True)
        emit({"cell": name, "variant": variant, "launched_by": launcher, "read_by": reader,
              "then": then, "start_wall": round(started, 3),
              "chrome_view": seen, **_summary(reads_path)})  # fmt: skip

    cell("0-input-only", "a", "runner", "fresh", then="input")
    for launcher, reader in CELLS:
        cell(f"L-{launcher}-R-{reader}", "b", launcher, reader)
    return 0


NAMED_CLASSES: Final = re.compile(
    r"Chrome_\w+|Intermediate D3D Window|ConsoleWindowClass|PseudoConsoleWindow"
    r"|OleMainThreadWndClass|IME|MSCTFIME UI|CicMarshalWndClass|GDI\+ Hook Window Class"
    r"|DDEMLEvent|Default IME|\w*Steer\w*"
)
"""Window classes named as themselves in the list: Chrome's, the console's and the system
helper windows a Python or Chrome process owns. Every other class is `class_other`."""


def window_row(  # noqa: PLR0913 - one window's facts, keyword-only
    *,
    z: int,
    hwnd: int,
    pid: int,
    relation: str,
    class_name: str,
    visible: bool,
    cloaked: bool | None,
    ex_style: int,
    minimized: bool,
    rect: tuple[int, int, int, int],
) -> dict[str, object]:
    """One top-level window as closed facts: its class only when it is Chrome's, the console's
    or a system helper's (`NAMED_CLASSES`) or the window belongs to this run, else `class_other`;
    never a title."""
    named = relation != "other" or NAMED_CLASSES.fullmatch(class_name) is not None
    return {
        "z": z,
        "hwnd": hwnd,
        "pid": pid,
        "relation": relation,
        "class": class_token(class_name) if named else "class_other",
        "visible": visible,
        "cloaked": cloaked,
        "layered": bool(ex_style & 0x80000),
        "transparent": bool(ex_style & 0x20),
        "tool": bool(ex_style & 0x80),
        "topmost": bool(ex_style & 0x8),
        "noactivate": bool(ex_style & 0x08000000),
        "minimized": minimized,
        "rect": list(rect),
    }


def _all_windows(ours: set[int], runner: int) -> list[dict[str, object]]:  # pragma: no cover
    """Every top-level window, top-down (EnumWindows), as `window_row`s."""
    import ctypes  # noqa: PLC0415 - Windows only
    from ctypes import wintypes  # noqa: PLC0415

    from steerai.probes.windows import is_cloaked  # noqa: PLC0415

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = (enum_proc, wintypes.LPARAM)
    user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    user32.GetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int)
    user32.GetWindowLongW.restype = ctypes.c_long
    user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
    for name in ("IsWindowVisible", "IsIconic"):
        getattr(user32, name).argtypes = (wintypes.HWND,)
    handles: list[int] = []

    def on_window(handle: int, _lparam: int) -> bool:
        handles.append(int(handle))
        return True

    user32.EnumWindows(enum_proc(on_window), 0)
    rows: list[dict[str, object]] = []
    for z, hwnd in enumerate(handles):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buffer, 256)
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        owner = int(pid.value)
        relation = "runner" if owner == runner else "ours" if owner in ours else "other"
        rows.append(
            window_row(
                z=z,
                hwnd=hwnd,
                pid=owner,
                relation=relation,
                class_name=buffer.value,
                visible=bool(user32.IsWindowVisible(hwnd)),
                cloaked=is_cloaked(hwnd),
                ex_style=int(user32.GetWindowLongW(hwnd, -20)) & 0xFFFFFFFF,
                minimized=bool(user32.IsIconic(hwnd)),
                rect=(rect.left, rect.top, rect.right, rect.bottom),
            )
        )
    return rows


def _display_state() -> str:  # pragma: no cover - live, Windows only
    """GUID_CONSOLE_DISPLAY_STATE as delivered on registration to a message-only window: `off`,
    `on`, `dimmed`, or `unread`."""
    import ctypes  # noqa: PLC0415 - Windows only
    import uuid  # noqa: PLC0415
    from ctypes import wintypes  # noqa: PLC0415

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    found: list[int] = []
    wndproc_t = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    )

    class _Setting(ctypes.Structure):
        _fields_ = (
            ("PowerSetting", ctypes.c_byte * 16),
            ("DataLength", wintypes.DWORD),
            ("Data", ctypes.c_ubyte * 4),
        )

    user32.DefWindowProcW.argtypes = (
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    )
    user32.DefWindowProcW.restype = ctypes.c_ssize_t

    def proc(hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        if msg == 0x0218 and wparam == 0x8013:  # WM_POWERBROADCAST, PBT_POWERSETTINGCHANGE
            setting = ctypes.cast(lparam, ctypes.POINTER(_Setting)).contents
            found.append(int(setting.Data[0]))
            return 1
        return int(user32.DefWindowProcW(hwnd, msg, wparam, lparam))

    callback = wndproc_t(proc)

    class _WndClass(ctypes.Structure):
        _fields_ = (
            ("style", wintypes.UINT), ("lpfnWndProc", wndproc_t), ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        )  # fmt: skip

    name = f"SteerDisplayProbe{uuid.uuid4().hex[:8]}"
    wc = _WndClass(lpfnWndProc=callback, lpszClassName=name)
    if not user32.RegisterClassW(ctypes.byref(wc)):
        return "unread"
    user32.CreateWindowExW.restype = wintypes.HWND
    hwnd = user32.CreateWindowExW(0, name, None, 0, 0, 0, 0, 0, wintypes.HWND(-3), None, None, None)
    if not hwnd:
        return "unread"
    guid = uuid.UUID("6fe69556-704a-47a0-8f24-c28d936fda47").bytes_le
    buffer = (ctypes.c_byte * 16).from_buffer_copy(guid)
    user32.RegisterPowerSettingNotification.restype = ctypes.c_void_p
    handle = user32.RegisterPowerSettingNotification(hwnd, ctypes.byref(buffer), 0)
    msg = wintypes.MSG()
    deadline = time.monotonic() + 2.0
    while not found and time.monotonic() < deadline:
        while user32.PeekMessageW(ctypes.byref(msg), hwnd, 0, 0, 1):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.05)
    if handle:
        user32.UnregisterPowerSettingNotification(ctypes.c_void_p(handle))
    user32.DestroyWindow(hwnd)
    user32.UnregisterClassW(name, None)
    if not found:
        return "unread"
    return {0: "off", 1: "on", 2: "dimmed"}.get(found[0], "unread")


def run_windows(out_dir: Path) -> int:  # noqa: PLR0915  # pragma: no cover - live
    """One input-only child on 3d-a; then, with a blank-prone Chrome on 3d-b up, the whole
    top-level window list, the display power state, the lock and the input desktop, and the
    Chrome's own visibility; then one Chrome on 3d-c-button with Chrome's native occlusion
    turned off (diagnostic only), its visibility and a plain read. Read-only."""
    import os  # noqa: PLC0415
    import shutil  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    from steerai.probes import instant_search as fixture  # noqa: PLC0415 - live only
    from steerai.probes.chrome_a11y import (  # noqa: PLC0415
        CHROME_DEFAULT,
        launch_chrome,
        stop_chrome,
    )
    from steerai.probes.layer_b import _ours  # noqa: PLC0415
    from steerai.probes.layer_b_pages import PageServer  # noqa: PLC0415
    from steerai.probes.process_tree import process_table, tree_pids  # noqa: PLC0415
    from steerai.probes.windows import (  # noqa: PLC0415
        input_desktop_name,
        session_locked,
        wait_window_for_pid,
    )

    runner = os.getpid()

    def emit(row: Mapping[str, object]) -> None:
        sys.stdout.write(json.dumps(dict(row)) + "\n")
        sys.stdout.flush()

    def visibility(cdp: Any) -> dict[str, object]:  # Any: a CdpSession
        def value(expression: str) -> object:
            reply = cdp.call("Runtime.evaluate", {"expression": expression, "returnByValue": True})
            return reply.get("result", {}).get("value")

        return {"visibilityState": value("document.visibilityState"),
                "hidden": value("document.hidden")}  # fmt: skip

    def chrome(name: str, variant: str, extra: tuple[str, ...], what: Any) -> None:
        run_dir = out_dir / name
        run_dir.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix="steer-readchild-"))
        with PageServer(fixture.pages()) as server:
            port = fixture._free_port()
            proc = launch_chrome(
                CHROME_DEFAULT, tmp / "profile", port, f"http://127.0.0.1:{server.port}/{variant}",
                extra_args=extra,
            )  # fmt: skip
            try:
                window = wait_window_for_pid(proc.pid, timeout_s=30.0, process=proc)
                cdp, _ = _ours(port, server.port)
                try:
                    fixture._wait_ready(cdp)
                    facts = what(window.hwnd, run_dir, proc.pid)
                    row = {"cell": name, "variant": variant, "flags": list(extra),
                           "bound_hwnd": window.hwnd, **facts}  # fmt: skip
                    row["chrome_view"] = visibility(cdp)
                finally:
                    cdp.close()
            finally:
                stop_chrome(proc, tmp / "profile")
                shutil.rmtree(tmp, ignore_errors=True)
        emit(row)

    def input_only(hwnd: int, run_dir: Path, _pid: int) -> dict[str, object]:
        subprocess.run(  # noqa: S603 - our own module, a fixed argv
            [sys.executable, "-m", "steerai.loop2.read_child", "child", "--hwnd", str(hwnd),
             "--arm", "mta", "--then", "input", "--out", str(run_dir / "reads.jsonl"),
             "--trace", str(run_dir / "trace.jsonl")],
            capture_output=True, timeout=CHILD_CAP_S, check=False,
        )  # fmt: skip
        return dict(_summary(run_dir / "reads.jsonl"))

    def plain(hwnd: int, run_dir: Path) -> dict[str, object]:
        subprocess.run(  # noqa: S603 - our own module, a fixed argv
            [sys.executable, "-m", "steerai.loop2.read_child", "plain", "--hwnd", str(hwnd),
             "--out", str(run_dir / "reads.jsonl")],
            capture_output=True, timeout=CHILD_CAP_S, check=False,
        )  # fmt: skip
        return dict(_summary(run_dir / "reads.jsonl"))

    def survey(hwnd: int, run_dir: Path, chrome_pid: int) -> dict[str, object]:
        table = process_table()
        ours = tree_pids(runner, table) | tree_pids(chrome_pid, table)
        windows = _all_windows(ours, runner)
        bound = next((w["z"] for w in windows if w["hwnd"] == hwnd), None)
        above = [w for w in windows if bound is not None and int(str(w["z"])) < int(str(bound))]
        return {
            "display": _display_state(),
            "session_locked": session_locked(),
            "input_desktop": input_desktop_name(),
            "bound_z": bound,
            "windows_total": len(windows),
            "above_bound": above,
            "runner_windows": [w for w in windows if w["relation"] == "runner"],
            "ours_windows": [w for w in windows if w["relation"] == "ours"],
            **plain(hwnd, run_dir),
        }

    def flagged(hwnd: int, run_dir: Path, _pid: int) -> dict[str, object]:
        return {"display": _display_state(), **plain(hwnd, run_dir)}

    chrome("0-input-only", "a", (), input_only)
    chrome("1-blank-survey", "b", (), survey)
    chrome("2-occlusion-off", "c-button", ("--disable-features=CalculateNativeWinOcclusion",),
           flagged)  # fmt: skip
    return 0


def _front_snapshot(hwnd: int, cdp: Any, label: str) -> dict[str, object]:  # pragma: no cover
    """At one point: the foreground (the bound window or other), the bound window's z, our own
    `cover_of`, every visible, unminimised window above it that overlaps it (z, relation, class
    token or `class_other`, rect; never a title), and Chrome's own visibilityState."""
    import ctypes  # noqa: PLC0415 - Windows only
    import os  # noqa: PLC0415

    from steerai.probes.windows import window_cover  # noqa: PLC0415
    from steerai.world.cover import cover_of  # noqa: PLC0415

    foreground = int(ctypes.windll.user32.GetForegroundWindow() or 0)
    windows = _all_windows(set(), os.getpid())
    bound = next((w for w in windows if w["hwnd"] == hwnd), None)
    z = None if bound is None else int(str(bound["z"]))
    rect = None if bound is None else bound["rect"]
    over: list[dict[str, object]] = []
    if z is not None and isinstance(rect, list):
        for w in windows:
            other = w["rect"]
            if int(str(w["z"])) >= z or not w["visible"] or w["minimized"] or w["cloaked"]:
                continue
            if not isinstance(other, list):
                continue
            if (
                other[0] < rect[2]
                and rect[0] < other[2]
                and other[1] < rect[3]
                and rect[1] < other[3]
            ):
                over.append(
                    {k: w[k] for k in ("z", "relation", "class", "tool", "topmost", "rect")}
                )
    try:
        cover: object = cover_of(window_cover(hwnd))
    except OSError as exc:
        cover = f"unread:{type(exc).__name__}"
    try:
        reply = cdp.call(
            "Runtime.evaluate", {"expression": "document.visibilityState", "returnByValue": True}
        )
        visibility: object = reply.get("result", {}).get("value")
    except Exception as exc:  # noqa: BLE001 - a diagnostic logs any DevTools failure as data
        visibility = f"unread:{type(exc).__name__}"
    return {
        "point": label,
        "foreground": "bound" if foreground == hwnd else "other",
        "bound_z": z,
        "cover_of": cover,
        "overlapping_above": over,
        "visibilityState": visibility,
    }


def run_front_child(  # pragma: no cover - live
    hwnd: int, *, cdp_port: int, server_port: int, out: Path, trace: Path
) -> int:
    """The real child's setup on `hwnd`, then the start's `bring_to_front` with its own result
    (and what v1's `acquire_foreground` returned under it), with a snapshot right before, right
    after, 1 s and 3 s after. No other input; never a model call."""
    from steerai.errors import SteerError  # noqa: PLC0415
    from steerai.loop2.decide_port import TaskGenerations  # noqa: PLC0415
    from steerai.loop2.live_world import (  # noqa: PLC0415
        LiveConfig,
        LiveWorld,
        WindowsPlatform,
        run_on_loop_thread,
    )
    from steerai.orchestrator.trace import JsonLinesTrace  # noqa: PLC0415
    from steerai.probes.layer_b import _ours  # noqa: PLC0415
    from steerai.probes.windows import process_name, window_pid  # noqa: PLC0415

    acquired: list[str] = []

    class Recording:
        """v1's InputPort, with what `acquire_foreground` returned or raised recorded."""

        def __init__(self, inner: Any) -> None:  # Any: v1's live InputPort
            self._inner = inner

        def __getattr__(self, name: str) -> Any:  # Any: the wrapped port's attribute
            return getattr(self._inner, name)

        def acquire_foreground(self, target: int) -> str:
            try:
                procedure = str(self._inner.acquire_foreground(target))
            except OSError as exc:
                acquired.append(f"refused:{type(exc).__name__}")
                raise
            acquired.append(procedure)
            return procedure

    class FrontPlatform(WindowsPlatform):
        def inputs(self, client: Any) -> Any:  # Any: the UiaClient and the wrapped port
            return Recording(super().inputs(client))

        def provider(self) -> Any:  # Any: a never-called `Call`
            return _never_called

        def counter(self) -> Any:  # Any: a never-called `Counter`
            return _never_called

    pid = window_pid(hwnd)
    config = LiveConfig(
        task_id="front-child",
        hwnd=hwnd,
        kill_key="ctrl+alt+k",
        trace_level="redacted",
        trace=JsonLinesTrace(trace),
        generations=TaskGenerations(),
        clock=time.monotonic,
        pause=time.sleep,
        bound_pid=pid,
        bound_image=process_name(pid).lower(),
    )
    platform = FrontPlatform()
    cdp, _ = _ours(cdp_port, server_port)

    def work() -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        with LiveWorld(config, platform) as world:
            rows.append(_front_snapshot(hwnd, cdp, "before_bring_to_front"))
            try:
                world.bring_to_front()
                result = "ok"
            except SteerError as exc:
                result = f"refused:{exc.message}"
            rows.append({"bring_to_front": result, "acquire_foreground": list(acquired)})
            rows.append(_front_snapshot(hwnd, cdp, "after_bring_to_front"))
            time.sleep(1.0)
            rows.append(_front_snapshot(hwnd, cdp, "after_1s"))
            time.sleep(2.0)
            rows.append(_front_snapshot(hwnd, cdp, "after_3s"))
        return rows

    try:
        rows = run_on_loop_thread(work, release=platform.release_com)
    finally:
        cdp.close()
    with out.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
    return 0


def run_front(out_dir: Path) -> int:  # pragma: no cover - live
    """One input-only child on 3d-a; then a fresh Chrome on 3d-b, a snapshot right after its
    launch, and the front child (`run_front_child`) on it."""
    import shutil  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    from steerai.probes import instant_search as fixture  # noqa: PLC0415 - live only
    from steerai.probes.chrome_a11y import (  # noqa: PLC0415
        CHROME_DEFAULT,
        launch_chrome,
        stop_chrome,
    )
    from steerai.probes.layer_b import _ours  # noqa: PLC0415
    from steerai.probes.layer_b_pages import PageServer  # noqa: PLC0415
    from steerai.probes.windows import wait_window_for_pid  # noqa: PLC0415

    def emit(row: Mapping[str, object]) -> None:
        sys.stdout.write(json.dumps(dict(row)) + "\n")
        sys.stdout.flush()

    for name, variant in (("0-input-only", "a"), ("1-front", "b")):
        run_dir = out_dir / name
        run_dir.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix="steer-readchild-"))
        with PageServer(fixture.pages()) as server:
            port = fixture._free_port()
            proc = launch_chrome(
                CHROME_DEFAULT, tmp / "profile", port, f"http://127.0.0.1:{server.port}/{variant}"
            )
            try:
                window = wait_window_for_pid(proc.pid, timeout_s=30.0, process=proc)
                cdp, _ = _ours(port, server.port)
                try:
                    fixture._wait_ready(cdp)
                    if name == "0-input-only":
                        argv = [sys.executable, "-m", "steerai.loop2.read_child", "child",
                                "--hwnd", str(window.hwnd), "--arm", "mta", "--then", "input",
                                "--out", str(run_dir / "reads.jsonl"),
                                "--trace", str(run_dir / "trace.jsonl")]  # fmt: skip
                        subprocess.run(argv, capture_output=True, timeout=CHILD_CAP_S, check=False)  # noqa: S603
                        emit({"cell": name, **_summary(run_dir / "reads.jsonl")})
                        continue
                    emit({"cell": name, **_front_snapshot(window.hwnd, cdp, "after_launch")})
                    argv = [sys.executable, "-m", "steerai.loop2.read_child", "front",
                            "--hwnd", str(window.hwnd), "--cdp-port", str(port),
                            "--server-port", str(server.port),
                            "--out", str(run_dir / "front.jsonl"),
                            "--trace", str(run_dir / "trace.jsonl")]  # fmt: skip
                    done = subprocess.run(  # noqa: S603 - our own module, a fixed argv
                        argv, capture_output=True, text=True, timeout=CHILD_CAP_S, check=False
                    )
                    tail = done.stderr[-300:] if done.returncode else ""
                    emit({"cell": name, "front_child_code": done.returncode, "stderr_tail": tail})
                    for line in (run_dir / "front.jsonl").read_text(encoding="utf-8").splitlines():
                        emit({"cell": name, **json.loads(line)})
                finally:
                    cdp.close()
            finally:
                stop_chrome(proc, tmp / "profile")
                shutil.rmtree(tmp, ignore_errors=True)
    return 0


_TEE: Final = (
    "import subprocess, sys; out = open(sys.argv[1], 'w', encoding='utf-8'); "
    "raise SystemExit(subprocess.call(sys.argv[2:], stdout=out, stderr=subprocess.DEVNULL))"
)
"""Runs the CLI with its stdout in a file (the harness's `_start` does the same)."""


def _summary(path: Path) -> dict[str, object]:  # pragma: no cover - live
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {"summary": None}
    rows = [json.loads(line) for line in lines if line.strip()]
    return rows[-1] if rows and "summary" in rows[-1] else {"summary": None}


def main(argv: Sequence[str] | None = None) -> int:  # noqa: PLR0911 - one per mode  # pragma: no cover
    parser = argparse.ArgumentParser(prog="python -m steerai.loop2.read_child")
    sub = parser.add_subparsers(dest="mode", required=True)
    child = sub.add_parser("child")
    child.add_argument("--hwnd", type=int, required=True)
    child.add_argument("--arm", choices=ARMS, required=True)
    child.add_argument("--out", type=Path, required=True)
    child.add_argument("--trace", type=Path, required=True)
    child.add_argument("--then", choices=THENS, default="none")
    plain = sub.add_parser("plain")
    plain.add_argument("--hwnd", type=int, required=True)
    plain.add_argument("--out", type=Path, required=True)
    front = sub.add_parser("front")
    front.add_argument("--hwnd", type=int, required=True)
    front.add_argument("--cdp-port", type=int, required=True)
    front.add_argument("--server-port", type=int, required=True)
    front.add_argument("--out", type=Path, required=True)
    front.add_argument("--trace", type=Path, required=True)
    front_run = sub.add_parser("front-run")
    front_run.add_argument("--out-dir", type=Path, required=True)
    windows = sub.add_parser("windows")
    windows.add_argument("--out-dir", type=Path, required=True)
    matrix = sub.add_parser("matrix")
    matrix.add_argument("--out-dir", type=Path, required=True)
    follow = sub.add_parser("follow")
    follow.add_argument("--exp", choices=EXPS, required=True)
    follow.add_argument("--out-dir", type=Path, required=True)
    follow.add_argument("--price-file", type=Path, required=True)
    sequence = sub.add_parser("sequence")
    sequence.add_argument("--arms", default="mta,sta")
    sequence.add_argument("--out-dir", type=Path, required=True)
    acting = sub.add_parser("acting")
    acting.add_argument("--mode", dest="acting_mode", choices=MODES, required=True)
    acting.add_argument("--out-dir", type=Path, required=True)
    acting.add_argument("--price-file", type=Path, required=True)
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        sys.stdout.write("read_child: windows only\n")
        return 0
    if args.mode == "child":
        return run_child_reads(
            args.hwnd, arm=args.arm, out=args.out, trace=args.trace, then=args.then
        )
    if args.mode == "plain":
        return run_plain(args.hwnd, out=args.out)
    if args.mode == "front":
        return run_front_child(
            args.hwnd,
            cdp_port=args.cdp_port,
            server_port=args.server_port,
            out=args.out,
            trace=args.trace,
        )
    if args.mode == "front-run":
        return run_front(args.out_dir)
    if args.mode == "windows":
        return run_windows(args.out_dir)
    if args.mode == "matrix":
        return run_matrix(args.out_dir)
    if args.mode == "follow":
        return run_follow(args.exp, args.out_dir, args.price_file)
    if args.mode == "acting":
        return run_acting(args.acting_mode, args.out_dir, args.price_file)
    return run_sequence(parse_arms(args.arms), args.out_dir)


if __name__ == "__main__":  # pragma: no cover - live
    raise SystemExit(main())
