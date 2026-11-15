"""[V2.62] addendum probe (diagnosis only; not a user flag, probe-only): does the start's nudge
bring the page back for a Chrome that starts behind another window, n >= 5, maximised windows
included, after the condition that strands it (a real v2 CLI child that sent input first).

Order, in one runner: a control (a fresh Chrome before any input: in front, so never nudged);
the trigger (the real v2 CLI on 3d-a, acting, as the harness runs it); then `RUNS` start
children on fresh Chromes on 3d-b, the last `MAXIMIZED` of them launched maximised. A start
child builds the real `LiveWorld` (session, hooks, tag), runs `bring_to_front` with its nudge,
then reads UIA every `READ_POLL_S` for `READ_S`; it never decides or acts. Each line: whether it
was nudged, maximised before and after, Chrome's own visibility after (DevTools, diagnostic only)
and when the page was first seen.

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
from typing import Any, Final

READ_S: Final = 10.0
READ_POLL_S: Final = 0.25
RUNS: Final = 7
MAXIMIZED: Final = 2
CHILD_CAP_S: Final = 90.0


def first_page(reads: Sequence[Mapping[str, Any]]) -> float | None:  # Any: JSON
    """The first read's time with something under the top Document; None when none had."""
    for read in reads:
        inside = read.get("doc_nodes")
        if isinstance(inside, int) and inside > 0:
            return float(read["t"])
    return None


def flicker_s(shows: Sequence[tuple[str, float]]) -> float | None:
    """Seconds from the minimise to the restore (or to the maximise after it); None when the
    window was never minimised."""
    times = dict(shows)
    if "minimize" not in times:
        return None
    end = times.get("maximize", times.get("restore"))
    return None if end is None else round(end - times["minimize"], 3)


def _never_called(_system: list[str], _blocks: list[str]) -> Any:  # Any: never returns
    raise RuntimeError("probe: this child never calls the model")


def run_start_child(hwnd: int, *, out: Path, trace: Path) -> int:  # pragma: no cover - live
    """The real start on `hwnd` (`LiveWorld`, `bring_to_front` with its nudge), then UIA reads
    only. Live, Windows only."""
    import ctypes  # noqa: PLC0415 - Windows only

    from steerai.errors import SteerError  # noqa: PLC0415
    from steerai.loop2.decide_port import TaskGenerations  # noqa: PLC0415
    from steerai.loop2.live_world import (  # noqa: PLC0415
        LiveConfig,
        LiveWorld,
        WindowsPlatform,
        run_on_loop_thread,
    )
    from steerai.orchestrator.trace import JsonLinesTrace  # noqa: PLC0415
    from steerai.probes.windows import process_name, window_pid  # noqa: PLC0415
    from steerai.world.observe2 import document_nodes  # noqa: PLC0415

    shows: list[tuple[str, float]] = []

    class ProbePlatform(WindowsPlatform):
        def show_window(self, hwnd: int, command: Any) -> None:  # Any: a ShowCommand
            super().show_window(hwnd, command)
            shows.append((str(command), time.monotonic()))

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
    zoomed = ctypes.windll.user32.IsZoomed

    def work() -> dict[str, object]:
        with LiveWorld(config, platform) as world:
            before = {
                "foreground": int(ctypes.windll.user32.GetForegroundWindow() or 0) == hwnd,
                "maximized": bool(zoomed(hwnd)),
            }
            try:
                world.bring_to_front()
                result = "ok"
            except SteerError as exc:
                result = exc.message  # the refusal token
            raised = time.monotonic()
            reads: list[dict[str, object]] = []
            while (now := time.monotonic() - raised) < READ_S:
                snapshot = world._poll_read().snapshot
                inside = document_nodes(snapshot)
                reads.append(
                    {"t": round(now, 3), "doc_nodes": None if inside is None else len(inside)}
                )
                time.sleep(READ_POLL_S)
            return {
                "before": before,
                "bring_to_front": result,
                "nudged": world.front_nudged,
                "maximized_after": bool(zoomed(hwnd)),
                "first_page_s": first_page(reads),
                "reads": len(reads),
                "shows": [name for name, _ in shows],
                "minimize_to_restore_s": flicker_s(shows),
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


def run(out_dir: Path, price: Path) -> int:  # noqa: PLR0915 - one live run  # pragma: no cover
    """The control, the trigger, then `RUNS` start children (see the module doc)."""
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
    from steerai.probes.chrome_a11y import (  # noqa: PLC0415
        CHROME_DEFAULT,
        launch_chrome,
        stop_chrome,
    )
    from steerai.probes.layer_b import _ours  # noqa: PLC0415
    from steerai.probes.layer_b_pages import PageServer  # noqa: PLC0415
    from steerai.probes.memory_guard import free_physical_mb  # noqa: PLC0415
    from steerai.probes.suite_chunks import kill_tree  # noqa: PLC0415
    from steerai.probes.windows import wait_window_for_pid  # noqa: PLC0415

    def emit(row: Mapping[str, object]) -> None:
        sys.stdout.write(json.dumps(dict(row)) + "\n")
        sys.stdout.flush()

    cells = [("0-control", "b", "start", False), ("1-trigger", "a", "trigger", False)] + [
        (f"{n + 2}-start{'-max' if n >= RUNS - MAXIMIZED else ''}", "b", "start",
         n >= RUNS - MAXIMIZED)
        for n in range(RUNS)
    ]  # fmt: skip
    for name, variant, kind, maximized in cells:
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
        with PageServer(fixture.pages()) as server:
            port = fixture._free_port()
            proc = launch_chrome(
                CHROME_DEFAULT, tmp / "profile", port, f"http://127.0.0.1:{server.port}/{variant}",
                extra_args=("--start-maximized",) if maximized else (),
            )  # fmt: skip
            cdp: Any = None  # Any: a CdpSession
            try:
                window = wait_window_for_pid(proc.pid, timeout_s=30.0, process=proc)
                cdp, _ = _ours(port, server.port)
                fixture._wait_ready(cdp)
                if kind == "trigger":
                    argv = child_argv(
                        THREE_D[0], hwnd=window.hwnd, price=price, trace=run_dir / "trace.jsonl"
                    )
                    ran = run_child(argv, cap_s=600.0, out=run_dir / "final.jsonl",
                                    start=_start, kill=kill_tree)  # fmt: skip
                    final = ran.final or {}
                    emit({"cell": name, **{k: final.get(k) for k in ("status", "reason", "used")}})
                    continue
                argv = [sys.executable, "-m", "steerai.loop2.unstick_probe", "child",
                        "--hwnd", str(window.hwnd), "--out", str(run_dir / "start.json"),
                        "--trace", str(run_dir / "trace.jsonl")]  # fmt: skip
                seen: list[tuple[float, object]] = []
                stop = threading.Event()

                watcher = threading.Thread(
                    target=_watch_visibility,
                    args=(port, server.port, time.monotonic(), stop, seen),
                )
                watcher.start()
                try:
                    done = subprocess.run(  # noqa: S603 - our own module, a fixed argv
                        argv, capture_output=True, text=True, timeout=CHILD_CAP_S, check=False
                    )
                finally:
                    stop.set()
                    watcher.join(timeout=5.0)
                reply = cdp.call(
                    "Runtime.evaluate",
                    {"expression": "document.visibilityState", "returnByValue": True},
                )
                row = json.loads((run_dir / "start.json").read_text("utf-8"))
                emit({"cell": name, "launched_maximized": maximized, "code": done.returncode,
                      **row, "chrome_view_end": reply.get("result", {}).get("value"),
                      "visibility_from_child_start": [f"{s}:{v}" for s, v in seen]})  # fmt: skip
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
        return run_start_child(args.hwnd, out=args.out, trace=args.trace)
    return run(args.out_dir, args.price_file)


if __name__ == "__main__":  # pragma: no cover - live
    raise SystemExit(main())
