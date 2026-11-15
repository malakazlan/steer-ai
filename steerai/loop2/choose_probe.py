"""The g12 diagnosis probe's v2 arm (`steerai.probes.choose_probe` runs it; diagnosis only,
probe-only): gallery row g12 chosen through the act port a `LiveWorld` hands the loop, on our own
throwaway Chrome, with no model call (a provider that is never called).

Run: `python -m steerai.loop2.choose_probe run --out-dir D`: one Chrome, the v1 arm (the probes
module's `arm`, v1's Session), a reload, then this module's own `arm`.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any


def _never_called(_system: list[str], _blocks: list[str]) -> Any:  # Any: never returns
    raise RuntimeError("probe: this arm never calls the model")


def _v2(hwnd: int, pid: int) -> dict[str, object]:  # pragma: no cover - live
    from steerai.loop2.decide_port import TaskGenerations  # noqa: PLC0415
    from steerai.loop2.decision import ActionDoc  # noqa: PLC0415
    from steerai.loop2.live_world import (  # noqa: PLC0415
        LiveConfig,
        LiveWorld,
        WindowsPlatform,
        run_on_loop_thread,
    )
    from steerai.loop2.loop import Dispatch  # noqa: PLC0415
    from steerai.loop2.task import Budgets, Envelope, Task  # noqa: PLC0415
    from steerai.orchestrator.trace import ListTrace  # noqa: PLC0415
    from steerai.probes.choose_probe import (  # noqa: PLC0415
        EVIDENCE_CUT,
        LABEL,
        OPTION,
        combo_rows,
        fact_data,
        pick_combo,
    )
    from steerai.session.monitor import DEFAULT_KILL_KEY  # noqa: PLC0415

    class ProbePlatform(WindowsPlatform):
        def provider(self) -> Any:  # Any: a never-called `Call`
            return _never_called

        def counter(self) -> Any:  # Any: a never-called `Counter`
            return _never_called

    config = LiveConfig(
        task_id="g12-probe",
        hwnd=hwnd,
        kill_key=DEFAULT_KILL_KEY,
        trace_level="redacted",
        trace=ListTrace(),
        generations=TaskGenerations(),
        bound_pid=pid,
        bound_image="chrome.exe",
    )
    task = Task(
        id="g12-probe",
        goal="probe",
        subgoals=(),
        envelope=Envelope(grants=(), mode="bootstrap"),
        budgets=Budgets(
            max_actions=1, max_model_calls=1, max_tokens=1, max_cost_usd=0.01, max_wall_s=60.0
        ),
        history=[],
    )
    platform = ProbePlatform()

    def work() -> dict[str, object]:
        with LiveWorld(config, platform) as world:
            world.bring_to_front()
            ports = world.ports(task)
            elements = ports.observe().snapshot.elements
            combos = combo_rows(elements, lambda e: fact_data(ports.read_value(e)))
            target, how = pick_combo(elements, LABEL)
            if target is None:
                return {"combos_before": combos, "picked": None}
            dispatch = Dispatch(
                action=ActionDoc(verb="choose", target=target.id, subgoal="g1", option=OPTION),
                runtime_id=target.runtime_id,
                role=target.role,
                name=target.name,
                risk="R1",
                provenance=None,
            )
            started = time.monotonic()
            try:
                done = ports.act(dispatch)
                result: dict[str, object] = {
                    "action": done.action.to_dict(),
                    "read_back": done.read_back,
                }
            except Exception as exc:  # noqa: BLE001 - a probe records any failure as data
                result = {"raised": f"{type(exc).__name__}: {exc}"[:EVIDENCE_CUT]}
            span = round(time.monotonic() - started, 3)
            return {
                "combos_before": combos,
                "picked": {"name": target.name, "how": how},
                "result": result,
                "s": span,
                "value_after": fact_data(ports.read_value(target)),
            }

    return run_on_loop_thread(work, release=platform.release_com)


def run(out_dir: Path) -> int:  # pragma: no cover - live
    """One Chrome: the v1 arm, a reload, the v2 arm."""
    import shutil  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    from steerai.probes.chrome_a11y import (  # noqa: PLC0415
        CHROME_DEFAULT,
        launch_chrome,
        stop_chrome,
    )
    from steerai.probes.gallery_page import pages  # noqa: PLC0415
    from steerai.probes.layer_b import _free_port, _ours  # noqa: PLC0415
    from steerai.probes.layer_b_pages import PageServer  # noqa: PLC0415
    from steerai.probes.windows import wait_window_for_pid  # noqa: PLC0415

    rows: list[dict[str, Any]] = []  # Any: JSON
    tmp = Path(tempfile.mkdtemp(prefix="steer-g12-"))
    with PageServer(pages()) as server:
        port = _free_port()
        proc = launch_chrome(
            CHROME_DEFAULT, tmp / "profile", port,
            f"http://127.0.0.1:{server.port}/page.html", window_size=(1100, 900),
        )  # fmt: skip
        try:
            window = wait_window_for_pid(proc.pid, timeout_s=30.0, process=proc)
            cdp, _ = _ours(port, server.port)
            cdp.close()
            time.sleep(1.0)
            for arm in ("v1", "v2"):
                if arm == "v2":
                    cdp, _ = _ours(port, server.port)
                    try:
                        cdp.call("Page.reload", {"ignoreCache": True})
                    finally:
                        cdp.close()
                    time.sleep(2.0)
                module = "steerai.probes.choose_probe" if arm == "v1" else __name__
                done = subprocess.run(  # noqa: S603 - our own modules, a fixed argv
                    [sys.executable, "-m", module, "arm",
                     "--hwnd", str(window.hwnd), "--pid", str(window.pid),
                     "--cdp-port", str(port), "--server-port", str(server.port)],
                    capture_output=True, text=True, timeout=180, check=False,
                )  # fmt: skip
                lines = [ln for ln in done.stdout.splitlines() if ln.strip()]
                row = json.loads(lines[-1]) if lines else {"arm": arm, "exit": done.returncode}
                if not lines:
                    row["stderr_tail"] = done.stderr.splitlines()[-15:]
                rows.append(row)
                sys.stdout.write(json.dumps(row) + "\n")
                sys.stdout.flush()
        finally:
            stop_chrome(proc, tmp / "profile")
            shutil.rmtree(tmp, ignore_errors=True)
    (out_dir / "choose_probe.json").write_text(json.dumps(rows))
    return 0


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - live
    from steerai.probes.choose_probe import add_arm_args, arm_row  # noqa: PLC0415

    parser = argparse.ArgumentParser(prog="python -m steerai.loop2.choose_probe")
    sub = parser.add_subparsers(dest="mode", required=True)
    add_arm_args(sub.add_parser("arm"))
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        sys.stdout.write("choose_probe: windows only\n")
        return 0
    return arm_row(
        "v2",
        lambda: _v2(args.hwnd, args.pid),
        hwnd=args.hwnd,
        cdp_port=args.cdp_port,
        server_port=args.server_port,
    )


if __name__ == "__main__":  # pragma: no cover - live
    raise SystemExit(main())
