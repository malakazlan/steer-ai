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


def _never_called(_system: list[str], _blocks: list[str]) -> NoReturn:
    raise RuntimeError("read_only: this child never calls the model")


# ---- the child (live) ---------------------------------------------------------------------------


def run_child_reads(hwnd: int, *, arm: Arm, out: Path, trace: Path) -> int:  # pragma: no cover
    """The real child's setup on `hwnd`, then reads only, in `arm`; one JSON line per read in
    `out`, then a summary line. Live, Windows only."""
    from steerai.loop2.decide_port import TaskGenerations  # noqa: PLC0415 - live only
    from steerai.loop2.live_world import (  # noqa: PLC0415
        LiveConfig,
        LiveWorld,
        WindowsPlatform,
        _ole32,
        run_on_loop_thread,
    )
    from steerai.loop2.loop import PortInterrupted, WindowCovered  # noqa: PLC0415
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
        handle.write(json.dumps({"summary": {"arm": arm, "first_page_s": first_page_s(reads),
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
    sequence = sub.add_parser("sequence")
    sequence.add_argument("--arms", default="mta,sta")
    sequence.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        sys.stdout.write("read_child: windows only\n")
        return 0
    if args.mode == "child":
        return run_child_reads(args.hwnd, arm=args.arm, out=args.out, trace=args.trace)
    return run_sequence(parse_arms(args.arms), args.out_dir)


if __name__ == "__main__":  # pragma: no cover - live
    raise SystemExit(main())
