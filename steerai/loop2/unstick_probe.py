"""[V2.64] evidence probe (diagnosis only; not a user flag, probe-only): the start's last-resort
nudge on a Chrome that starts behind another window, after the condition that strands it (a real
v2 CLI child that sent input first). At least `RUNS` runs per window kind (normal, maximised,
half-screen) and nudge (`move1px`, `minrestore`; a maximised window takes `minrestore` only,
since `move1px` falls back to it there).

Order, in one runner: a control (a fresh Chrome before any input: in front, never nudged); the
trigger (the real v2 CLI on 3d-a, acting, as the harness runs it); then the start children, each
on a fresh Chrome on 3d-b. A start child builds the real `LiveWorld` (session, hooks, tag), runs
`bring_to_front` with the given nudge, then reads UIA every `READ_POLL_S` for `READ_S`; it never
decides or acts. Each line: the nudge applied (or none), the placement and rect before and after
and whether they match, the time from the first nudge call to the last, Chrome's own visibility
over time (DevTools, diagnostic only) and when the page was first seen.

"Half-screen" is a window set by this probe to the work area's left half (SetWindowPos on our own
window): a true Aero Snap needs keyboard input, which the probe never sends, so snapping itself
stays unmeasured.

Run: `python -m steerai.loop2.unstick_probe run --out-dir D --price-file P` (the trigger needs
the API key in the environment).

#400 REVISE, two more modes, no trigger and no model call:
- `docwait`: `doc_plan()` starts, cold (the child starts as soon as the window is listed) and
  loaded (after the page reports loaded), each on a fresh Chrome on 3d-b with the window in
  front. The child raises the window as the start does, then reads UIA every `DOC_POLL_S` up to
  `DOC_READ_S`: when the top Document first appears and when something is first under it. The
  wait is `wait_from` (the p99 plus `MARGIN_S`). Chrome's own visibility is kept beside each
  start, so a start that went hidden is told apart from a slow paint.
- `placement`: one maximised Chrome, the real `minrestore` nudge `PLACEMENT_RUNS` times, the raw
  WINDOWPLACEMENT (flags included) and the rect before and after each.

Run: `python -m steerai.loop2.unstick_probe docwait --out-dir D` and `... placement --out-dir D`.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final, Literal, get_args

READ_S: Final = 10.0
READ_POLL_S: Final = 0.25
RUNS: Final = 5
DOC_STARTS: Final = 24
DOC_READ_S: Final = 15.0
DOC_POLL_S: Final = 0.05
MARGIN_S: Final = 0.5
PLACEMENT_RUNS: Final = 5
StartKind = Literal["cold", "loaded"]
CHILD_CAP_S: Final = 90.0
WindowKind = Literal["normal", "maximized", "half"]
KINDS: Final[tuple[WindowKind, ...]] = get_args(WindowKind)
NUDGES_FOR: Final[Mapping[WindowKind, tuple[str, ...]]] = {
    "normal": ("move1px", "minrestore"),
    "half": ("move1px", "minrestore"),
    "maximized": ("minrestore",),
}


def first_page(reads: Sequence[Mapping[str, Any]]) -> float | None:  # Any: JSON
    """The first read's time with something under the top Document; None when none had."""
    for read in reads:
        inside = read.get("doc_nodes")
        if isinstance(inside, int) and inside > 0:
            return float(read["t"])
    return None


def doc_times(reads: Sequence[Mapping[str, Any]]) -> dict[str, float | None]:  # Any: JSON
    """When the top Document first appeared (even empty) and when something was first under it;
    None for either never seen."""
    document: float | None = None
    for read in reads:
        if read.get("doc_nodes") is not None:
            document = float(read["t"])
            break
    return {"first_document_s": document, "first_page_s": first_page(reads)}


def p99(values: Sequence[float]) -> float:
    """The nearest-rank 99th percentile."""
    ordered = sorted(values)
    return ordered[math.ceil(0.99 * len(ordered)) - 1]


def wait_from(first_pages: Sequence[float], *, margin_s: float) -> float:
    """#400 REVISE 2: `DOCUMENT_WAIT_S` from the measured times to a page: the p99 plus the
    margin, rounded up to a tenth of a second."""
    if not first_pages:
        raise ValueError("no page was seen: the wait cannot be set")
    return math.ceil(round((p99(first_pages) + margin_s) * 10, 6)) / 10


def put_back_same(before: Sequence[object], after: Sequence[object]) -> bool:
    """A placement put back: (flags, show, normal, min, max) equal but the minimised position,
    which Windows fills in on a window's first minimise (-1, -1 is unset)."""
    return [before[i] for i in (0, 1, 2, 4)] == [after[i] for i in (0, 1, 2, 4)]


def doc_plan() -> list[tuple[str, StartKind]]:
    """`DOC_STARTS` starts, cold and loaded taking turns."""
    kinds: tuple[StartKind, StartKind] = ("cold", "loaded")
    return [(f"{kinds[n % 2]}-{n // 2 + 1}", kinds[n % 2]) for n in range(DOC_STARTS)]


def nudge_span_s(calls: Sequence[tuple[str, float]]) -> float | None:
    """Seconds from the first nudge call to the last (the flicker or the move); None when the
    window was never nudged."""
    if not calls:
        return None
    return round(calls[-1][1] - calls[0][1], 3)


def plan() -> list[tuple[str, WindowKind, str]]:
    """The start cells after the trigger: (name, window kind, nudge), `RUNS` of each pair."""
    cells: list[tuple[str, WindowKind, str]] = []
    for kind in KINDS:
        for nudge in NUDGES_FOR[kind]:
            cells += [(f"{kind}-{nudge}-{n}", kind, nudge) for n in range(1, RUNS + 1)]
    return cells


def _never_called(_system: list[str], _blocks: list[str]) -> Any:  # Any: never returns
    raise RuntimeError("probe: this child never calls the model")


def run_start_child(hwnd: int, *, nudge: str, out: Path, trace: Path) -> int:  # pragma: no cover
    """The real start on `hwnd` with `nudge`, then UIA reads only. Live, Windows only."""
    from steerai.errors import SteerError  # noqa: PLC0415
    from steerai.loop2.decide_port import TaskGenerations  # noqa: PLC0415
    from steerai.loop2.live_world import (  # noqa: PLC0415
        LiveConfig,
        LiveWorld,
        WindowsPlatform,
        run_on_loop_thread,
    )
    from steerai.orchestrator.trace import JsonLinesTrace  # noqa: PLC0415
    from steerai.probes.windows import (  # noqa: PLC0415
        get_placement,
        get_window_rect,
        process_name,
        window_pid,
    )
    from steerai.world.observe2 import document_nodes  # noqa: PLC0415

    calls: list[tuple[str, float]] = []

    class ProbePlatform(WindowsPlatform):
        def show_window(self, hwnd: int, command: Any) -> None:  # Any: a ShowCommand
            calls.append((f"show:{command}", time.monotonic()))
            super().show_window(hwnd, command)

        def set_window_placement(self, hwnd: int, placement: Any) -> None:  # Any: a placement
            super().set_window_placement(hwnd, placement)
            calls.append(("placement", time.monotonic()))

        def set_window_rect(self, hwnd: int, rect: tuple[int, int, int, int]) -> None:
            calls.append(("rect", time.monotonic()))
            super().set_window_rect(hwnd, rect)

        def provider(self) -> Any:  # Any: a never-called `Call`
            return _never_called

        def counter(self) -> Any:  # Any: a never-called `Counter`
            return _never_called

    pid = window_pid(hwnd)
    config = LiveConfig(
        task_id="unstick-probe",
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
    platform = ProbePlatform()

    def work() -> dict[str, object]:
        with LiveWorld(config, platform) as world:
            placement_before, rect_before = get_placement(hwnd), get_window_rect(hwnd)
            try:
                world.bring_to_front(nudge=nudge)  # type: ignore[arg-type]  # a probe's choice
                result = "ok"
            except SteerError as exc:
                result = exc.message  # the refusal token
            started = time.monotonic()
            reads: list[dict[str, object]] = []
            while (now := time.monotonic() - started) < READ_S:
                inside = document_nodes(world._poll_read().snapshot)
                reads.append(
                    {"t": round(now, 3), "doc_nodes": None if inside is None else len(inside)}
                )
                time.sleep(READ_POLL_S)
            placement_after, rect_after = get_placement(hwnd), get_window_rect(hwnd)
            return {
                "bring_to_front": result,
                "nudged": world.front_nudged,
                "calls": [name for name, _ in calls],
                "nudge_span_s": nudge_span_s(calls),
                "show_cmd_before": placement_before[1],
                "placement_same": put_back_same(placement_before, placement_after),
                "rect_same": rect_after == rect_before,
                "first_page_s": first_page(reads),
                "reads": len(reads),
            }

    row = run_on_loop_thread(work, release=platform.release_com)
    out.write_text(json.dumps(row) + "\n", encoding="utf-8")
    return 0


def _probe_world(hwnd: int, trace: Path) -> tuple[Any, Any]:  # pragma: no cover - live
    """A LiveWorld config and platform for a probe child on our own `hwnd`: the real session,
    hooks and tag, and a provider that is never called. Any: LiveConfig, WindowsPlatform."""
    from steerai.loop2.decide_port import TaskGenerations  # noqa: PLC0415
    from steerai.loop2.live_world import LiveConfig, WindowsPlatform  # noqa: PLC0415
    from steerai.orchestrator.trace import JsonLinesTrace  # noqa: PLC0415
    from steerai.probes.windows import process_name, window_pid  # noqa: PLC0415

    class ProbePlatform(WindowsPlatform):
        def provider(self) -> Any:  # Any: a never-called `Call`
            return _never_called

        def counter(self) -> Any:  # Any: a never-called `Counter`
            return _never_called

    pid = window_pid(hwnd)
    config = LiveConfig(
        task_id="unstick-probe",
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
    return config, ProbePlatform()


def run_doc_child(hwnd: int, *, out: Path, trace: Path) -> int:  # pragma: no cover - live
    """#400 REVISE 2: raise our window as the start does, never nudge, then read UIA every
    `DOC_POLL_S` until a page has been seen for a second, or `DOC_READ_S`."""
    from steerai.loop2.live_world import LiveWorld, run_on_loop_thread  # noqa: PLC0415
    from steerai.probes.win_input import acquire_foreground  # noqa: PLC0415
    from steerai.world.observe2 import document_nodes  # noqa: PLC0415

    config, platform = _probe_world(hwnd, trace)

    def work() -> dict[str, object]:
        with LiveWorld(config, platform) as world:
            how = acquire_foreground(hwnd)
            started = time.monotonic()
            reads: list[dict[str, object]] = []
            seen_at: float | None = None
            while (now := time.monotonic() - started) < DOC_READ_S:
                inside = document_nodes(world._poll_read().snapshot)
                reads.append(
                    {"t": round(now, 3), "doc_nodes": None if inside is None else len(inside)}
                )
                if inside and seen_at is None:
                    seen_at = now
                if seen_at is not None and now - seen_at >= 1.0:
                    break
                time.sleep(DOC_POLL_S)
            return {"acquire": how, **doc_times(reads), "reads": len(reads)}

    row = run_on_loop_thread(work, release=platform.release_com)
    out.write_text(json.dumps(row) + "\n", encoding="utf-8")
    return 0


def run_placement_child(hwnd: int, *, out: Path, trace: Path) -> int:  # pragma: no cover - live
    """#400 REVISE 3: the real `minrestore` nudge `PLACEMENT_RUNS` times on our own maximised
    window, the raw placement (flags included) and the rect before and after each."""
    from steerai.loop2.live_world import LiveWorld, run_on_loop_thread  # noqa: PLC0415
    from steerai.probes.windows import get_placement, get_window_rect  # noqa: PLC0415

    config, platform = _probe_world(hwnd, trace)

    def work() -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        with LiveWorld(config, platform) as world:
            for run in range(1, PLACEMENT_RUNS + 1):
                before, rect_before = get_placement(hwnd), get_window_rect(hwnd)
                started = time.monotonic()
                world._nudge(hwnd, "minrestore")
                span = round(time.monotonic() - started, 3)
                time.sleep(0.5)
                after, rect_after = get_placement(hwnd), get_window_rect(hwnd)
                fields = ("flags", "showCmd", "rcNormalPosition", "ptMinPosition", "ptMaxPosition")
                rows.append({
                    "run": run, "span_s": span, "nudged": world.front_nudged,
                    "before": list(before), "after": list(after),
                    "differs": [f for f, a, b in zip(fields, before, after, strict=True) if a != b],
                    "rect_same": rect_before == rect_after,
                })  # fmt: skip
                time.sleep(0.5)
        return rows

    rows = run_on_loop_thread(work, release=platform.release_com)
    out.write_text(json.dumps(rows) + "\n", encoding="utf-8")
    return 0


def run_docwait(out_dir: Path) -> int:  # noqa: PLR0915 - one live run  # pragma: no cover
    """The `doc_plan()` starts, each on a fresh Chrome on 3d-b, then the wait from them."""
    import shutil  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    import threading  # noqa: PLC0415

    from steerai.loop2.stage1 import _idle_s, precheck, wait_until_ready  # noqa: PLC0415
    from steerai.probes import instant_search as fixture  # noqa: PLC0415 - live only
    from steerai.probes.chrome_a11y import stop_chrome  # noqa: PLC0415
    from steerai.probes.layer_b import _ours  # noqa: PLC0415
    from steerai.probes.layer_b_pages import PageServer  # noqa: PLC0415
    from steerai.probes.memory_guard import free_physical_mb  # noqa: PLC0415
    from steerai.probes.windows import wait_window_for_pid  # noqa: PLC0415

    pages: list[float] = []
    for name, kind in doc_plan():
        refused = wait_until_ready(
            lambda: precheck(free_mb=free_physical_mb, idle_s=_idle_s),
            sleep=time.sleep, clock=time.monotonic,
        )  # fmt: skip
        if refused is not None:
            _emit({"start": name, "not_started": refused})
            continue
        run_dir = out_dir / name
        run_dir.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix="steer-unstick-"))
        with PageServer(fixture.pages()) as server:
            port = fixture._free_port()
            proc = _launch("normal", tmp / "profile", port, f"http://127.0.0.1:{server.port}/b")
            try:
                window = wait_window_for_pid(proc.pid, timeout_s=30.0, process=proc)
                if kind == "loaded":
                    cdp, _ = _ours(port, server.port)
                    try:
                        fixture._wait_ready(cdp)
                    finally:
                        cdp.close()
                seen: list[tuple[float, object]] = []
                stop = threading.Event()
                watcher = threading.Thread(
                    target=_watch_visibility, args=(port, server.port, time.monotonic(), stop, seen)
                )
                watcher.start()
                try:
                    done = subprocess.run(  # noqa: S603 - our own module, a fixed argv
                        [sys.executable, "-m", "steerai.loop2.unstick_probe", "docchild",
                         "--hwnd", str(window.hwnd), "--out", str(run_dir / "doc.json"),
                         "--trace", str(run_dir / "trace.jsonl")],
                        capture_output=True, text=True, timeout=CHILD_CAP_S, check=False,
                    )  # fmt: skip
                finally:
                    stop.set()
                    watcher.join(timeout=5.0)
                row = json.loads((run_dir / "doc.json").read_text("utf-8"))
                if isinstance(row.get("first_page_s"), float):
                    pages.append(row["first_page_s"])
                _emit({"start": name, "kind": kind, "code": done.returncode, **row,
                       "visibility": [f"{t}:{v}" for t, v in seen]})  # fmt: skip
            finally:
                stop_chrome(proc, tmp / "profile")
                shutil.rmtree(tmp, ignore_errors=True)
    summary: dict[str, object] = {"starts": len(pages), "pages": sorted(pages)}
    if pages:
        summary |= {"p99": p99(pages), "wait_s": wait_from(pages, margin_s=MARGIN_S)}
    _emit({"summary": summary})
    return 0


def run_placement(out_dir: Path) -> int:  # pragma: no cover - live
    """One maximised Chrome on 3d-b, the placement child on it."""
    import shutil  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    from steerai.probes import instant_search as fixture  # noqa: PLC0415 - live only
    from steerai.probes.chrome_a11y import stop_chrome  # noqa: PLC0415
    from steerai.probes.layer_b_pages import PageServer  # noqa: PLC0415
    from steerai.probes.windows import wait_window_for_pid  # noqa: PLC0415

    tmp = Path(tempfile.mkdtemp(prefix="steer-unstick-"))
    with PageServer(fixture.pages()) as server:
        port = fixture._free_port()
        proc = _launch("maximized", tmp / "profile", port, f"http://127.0.0.1:{server.port}/b")
        try:
            window = wait_window_for_pid(proc.pid, timeout_s=30.0, process=proc)
            time.sleep(2.0)
            done = subprocess.run(  # noqa: S603 - our own module, a fixed argv
                [sys.executable, "-m", "steerai.loop2.unstick_probe", "placementchild",
                 "--hwnd", str(window.hwnd), "--out", str(out_dir / "placement.json"),
                 "--trace", str(out_dir / "placement-trace.jsonl")],
                capture_output=True, text=True, timeout=CHILD_CAP_S, check=False,
            )  # fmt: skip
            rows = json.loads((out_dir / "placement.json").read_text("utf-8"))
            for row in rows:
                _emit(row)
            _emit({"code": done.returncode})
        finally:
            stop_chrome(proc, tmp / "profile")
            shutil.rmtree(tmp, ignore_errors=True)
    return 0


def _emit(row: Mapping[str, object]) -> None:  # pragma: no cover - live
    sys.stdout.write(json.dumps(dict(row)) + "\n")
    sys.stdout.flush()


def _watch_visibility(  # pragma: no cover - live
    port: int, server_port: int, started: float, stop: Any, seen: list[tuple[float, object]]
) -> None:
    """Chrome's own visibilityState over DevTools every 100 ms until `stop` is set, each change
    with its time from `started` (diagnostic only). Any: a threading.Event."""
    from steerai.probes.layer_b import _ours  # noqa: PLC0415 - live only

    session, _ = _ours(port, server_port)
    try:
        while not stop.is_set():
            reply = session.call(
                "Runtime.evaluate",
                {"expression": "document.visibilityState", "returnByValue": True},
            )
            state = reply.get("result", {}).get("value")
            if not seen or seen[-1][1] != state:
                seen.append((round(time.monotonic() - started, 3), state))
            time.sleep(0.1)
    finally:
        session.close()


def _launch(kind: WindowKind, profile: Path, port: int, url: str) -> Any:  # pragma: no cover
    """A throwaway Chrome of `kind` (Any: a Popen). `launch_chrome` fixes size and position, which
    overrides a maximised start, so a maximised Chrome gets the same flags but those."""
    import subprocess  # noqa: PLC0415

    from steerai.probes.chrome_a11y import CHROME_DEFAULT, launch_chrome  # noqa: PLC0415

    if kind == "maximized":
        return subprocess.Popen(  # noqa: S603 - our own Chrome, a fixed argv
            [str(CHROME_DEFAULT), f"--user-data-dir={profile}", f"--remote-debugging-port={port}",
             "--no-first-run", "--no-default-browser-check", "--disable-sync",
             "--start-maximized", url]
        )  # fmt: skip
    return launch_chrome(CHROME_DEFAULT, profile, port, url)


def _half_screen(hwnd: int) -> None:  # pragma: no cover - live, Windows only
    """Our own window to the work area's left half (SPI_GETWORKAREA), no activation."""
    import ctypes  # noqa: PLC0415 - Windows only
    from ctypes import wintypes  # noqa: PLC0415

    from steerai.probes.windows import set_window_rect  # noqa: PLC0415

    area = wintypes.RECT()
    ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(area), 0)
    set_window_rect(hwnd, (area.left, area.top, (area.left + area.right) // 2, area.bottom))


def run(out_dir: Path, price: Path) -> int:  # noqa: PLR0915 - one live run  # pragma: no cover
    """The control, the trigger, then the `plan()` cells (see the module doc)."""
    import shutil  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    import threading  # noqa: PLC0415

    from steerai.loop2.stage1 import (  # noqa: PLC0415
        THREE_D,
        _idle_s,
        _start,
        child_argv,
        precheck,
        run_child,
        wait_until_ready,
    )
    from steerai.probes import instant_search as fixture  # noqa: PLC0415 - live only
    from steerai.probes.chrome_a11y import stop_chrome  # noqa: PLC0415
    from steerai.probes.layer_b import _ours  # noqa: PLC0415
    from steerai.probes.layer_b_pages import PageServer  # noqa: PLC0415
    from steerai.probes.memory_guard import free_physical_mb  # noqa: PLC0415
    from steerai.probes.suite_chunks import kill_tree  # noqa: PLC0415
    from steerai.probes.win_input import ensure_per_monitor_v2  # noqa: PLC0415
    from steerai.probes.windows import wait_window_for_pid  # noqa: PLC0415

    ensure_per_monitor_v2()  # the half-screen rect in the same pixels the child reads

    def emit(row: Mapping[str, object]) -> None:
        sys.stdout.write(json.dumps(dict(row)) + "\n")
        sys.stdout.flush()

    cells: list[tuple[str, WindowKind, str]] = [
        ("0-control", "normal", "minrestore"),
        ("1-trigger", "normal", "trigger"),
        *plan(),
    ]
    for name, kind, nudge in cells:
        refused = wait_until_ready(
            lambda: precheck(free_mb=free_physical_mb, idle_s=_idle_s),
            sleep=time.sleep, clock=time.monotonic,
        )  # fmt: skip
        if refused is not None:
            emit({"cell": name, "not_started": refused})
            continue
        run_dir = out_dir / name
        run_dir.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix="steer-unstick-"))
        variant = "a" if nudge == "trigger" else "b"
        with PageServer(fixture.pages()) as server:
            port = fixture._free_port()
            proc = _launch(kind, tmp / "profile", port, f"http://127.0.0.1:{server.port}/{variant}")
            cdp: Any = None  # Any: a CdpSession
            try:
                window = wait_window_for_pid(proc.pid, timeout_s=30.0, process=proc)
                cdp, _ = _ours(port, server.port)
                fixture._wait_ready(cdp)
                if name == "0-control":
                    emit({"chrome": cdp.call("Browser.getVersion", {}).get("product")})
                if nudge == "trigger":
                    argv = child_argv(
                        THREE_D[0], hwnd=window.hwnd, price=price, trace=run_dir / "trace.jsonl"
                    )
                    ran = run_child(argv, cap_s=600.0, out=run_dir / "final.jsonl",
                                    start=_start, kill=kill_tree)  # fmt: skip
                    final = ran.final or {}
                    emit({"cell": name, **{k: final.get(k) for k in ("status", "reason", "used")}})
                    continue
                if kind == "half":
                    _half_screen(window.hwnd)
                    time.sleep(0.3)
                argv = [sys.executable, "-m", "steerai.loop2.unstick_probe", "child",
                        "--hwnd", str(window.hwnd), "--nudge", nudge,
                        "--out", str(run_dir / "start.json"),
                        "--trace", str(run_dir / "trace.jsonl")]  # fmt: skip
                seen: list[tuple[float, object]] = []
                stop = threading.Event()
                watcher = threading.Thread(
                    target=_watch_visibility, args=(port, server.port, time.monotonic(), stop, seen)
                )
                watcher.start()
                try:
                    done = subprocess.run(  # noqa: S603 - our own module, a fixed argv
                        argv, capture_output=True, text=True, timeout=CHILD_CAP_S, check=False
                    )
                finally:
                    stop.set()
                    watcher.join(timeout=5.0)
                row = json.loads((run_dir / "start.json").read_text("utf-8"))
                emit({"cell": name, "kind": kind, "nudge_asked": nudge, "code": done.returncode,
                      **row, "visibility": [f"{s}:{v}" for s, v in seen]})  # fmt: skip
            finally:
                if cdp is not None:
                    cdp.close()
                stop_chrome(proc, tmp / "profile")
                shutil.rmtree(tmp, ignore_errors=True)
    return 0


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - live
    parser = argparse.ArgumentParser(prog="python -m steerai.loop2.unstick_probe")
    sub = parser.add_subparsers(dest="mode", required=True)
    child = sub.add_parser("child")
    child.add_argument("--hwnd", type=int, required=True)
    child.add_argument("--nudge", choices=("move1px", "minrestore"), required=True)
    child.add_argument("--out", type=Path, required=True)
    child.add_argument("--trace", type=Path, required=True)
    for name in ("docchild", "placementchild"):
        other = sub.add_parser(name)
        other.add_argument("--hwnd", type=int, required=True)
        other.add_argument("--out", type=Path, required=True)
        other.add_argument("--trace", type=Path, required=True)
    whole = sub.add_parser("run")
    whole.add_argument("--out-dir", type=Path, required=True)
    whole.add_argument("--price-file", type=Path, required=True)
    for name in ("docwait", "placement"):
        sub.add_parser(name).add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        sys.stdout.write("unstick_probe: windows only\n")
        return 0
    if args.mode == "child":
        return run_start_child(args.hwnd, nudge=args.nudge, out=args.out, trace=args.trace)
    if args.mode == "docchild":
        return run_doc_child(args.hwnd, out=args.out, trace=args.trace)
    if args.mode == "placementchild":
        return run_placement_child(args.hwnd, out=args.out, trace=args.trace)
    if args.mode in ("docwait", "placement"):
        args.out_dir.mkdir(parents=True, exist_ok=True)
        return run_docwait(args.out_dir) if args.mode == "docwait" else run_placement(args.out_dir)
    return run(args.out_dir, args.price_file)


if __name__ == "__main__":  # pragma: no cover - live
    raise SystemExit(main())
