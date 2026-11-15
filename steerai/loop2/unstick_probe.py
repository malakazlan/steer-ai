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
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final, Literal, get_args

READ_S: Final = 10.0
READ_POLL_S: Final = 0.25
RUNS: Final = 5
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

        def move_window_by(self, hwnd: int, dx: int) -> None:
            calls.append((f"move:{dx:+d}", time.monotonic()))
            super().move_window_by(hwnd, dx)

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
                "show_cmd_before": placement_before[0],
                "placement_same": placement_after == placement_before,
                "rect_same": rect_after == rect_before,
                "first_page_s": first_page(reads),
                "reads": len(reads),
            }

    row = run_on_loop_thread(work, release=platform.release_com)
    out.write_text(json.dumps(row) + "\n", encoding="utf-8")
    return 0


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
    whole = sub.add_parser("run")
    whole.add_argument("--out-dir", type=Path, required=True)
    whole.add_argument("--price-file", type=Path, required=True)
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        sys.stdout.write("unstick_probe: windows only\n")
        return 0
    if args.mode == "child":
        return run_start_child(args.hwnd, nudge=args.nudge, out=args.out, trace=args.trace)
    return run(args.out_dir, args.price_file)


if __name__ == "__main__":  # pragma: no cover - live
    raise SystemExit(main())
