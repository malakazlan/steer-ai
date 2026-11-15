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
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final, Literal, NoReturn, get_args

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


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - live
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
    if args.mode == "follow":
        return run_follow(args.exp, args.out_dir, args.price_file)
    if args.mode == "acting":
        return run_acting(args.acting_mode, args.out_dir, args.price_file)
    return run_sequence(parse_arms(args.arms), args.out_dir)


if __name__ == "__main__":  # pragma: no cover - live
    raise SystemExit(main())
