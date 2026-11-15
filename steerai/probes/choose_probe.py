"""Gallery row g12 (`<select>` "Country", choose "Kenya"), diagnosis only, probe-only: why v1's
Session failed it in every arm of the [V2.57a] apartment comparison, and whether v2's act port
fails it too.

One throwaway Chrome on the gallery page. Arm `v1`: the ComboBoxes as UIA names them and their
live values, then the one step through v1's own Session (its executor built as v1's run builds
it, with the option source: without one a choose refuses `no_option_source`, 2026-11-15), its
whole result kept (gate outcome, codes, the action and the evidence). The page is reloaded.
Arm `v2`: the same read, then the same single step through v2's `ActPort` on a `LiveWorld` (no
model call), its `ActResult` and the value read after. Each arm runs in its own child process
(one COM apartment each) and first shows the page (raise; the 1-px nudge only if Chrome holds
it hidden). The page's own log gives the g12 events after each arm. Sends real input to our own
window only.

Run: `python -m steerai.probes.choose_probe run --out-dir D`.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final, Literal

from steerai.world.elements import Element

ROW: Final = "g12"
LABEL: Final = "Country"
OPTION: Final = "Kenya"
EVIDENCE_CUT: Final = 200
Arm = Literal["v1", "v2"]


def combo_rows(
    elements: Sequence[Element], value_of: Callable[[Element], object]
) -> list[dict[str, object]]:
    """Every ComboBox as UIA lists it, with its live value as read (None when unread)."""
    return [
        {
            "id": e.id,
            "name": e.name,
            "automation_id": e.automation_id,
            "value": value_of(e),
            "runtime_id": list(e.runtime_id),
        }
        for e in elements
        if e.role == "ComboBox"
    ]


def pick_combo(
    elements: Sequence[Element], label: str
) -> tuple[Element | None, Literal["exact", "prefix"] | None]:
    """The ComboBox named `label` exactly; else the only one whose name starts with it."""
    combos = [e for e in elements if e.role == "ComboBox"]
    exact = [e for e in combos if e.name == label]
    if len(exact) == 1:
        return exact[0], "exact"
    prefixed = [e for e in combos if e.name.startswith(label)]
    if len(prefixed) == 1:
        return prefixed[0], "prefix"
    return None, None


def step_record(raw: Mapping[str, Any]) -> dict[str, object]:  # Any: StepResult.to_dict()
    """v1's step result kept whole but its evidence, each value as text cut to 200 characters."""
    evidence = raw.get("evidence") or {}
    return {
        "ok": raw.get("ok"),
        "gate": raw.get("gate"),
        "code": raw.get("code"),
        "verify_code": raw.get("verify_code"),
        "label_kind": raw.get("label_kind"),
        "action": raw.get("action"),
        "latency_ms": raw.get("latency_ms"),
        "evidence": {
            str(k): (v if isinstance(v, str) else str(v))[:EVIDENCE_CUT]
            for k, v in evidence.items()
        },
    }


# ---- live ---------------------------------------------------------------------------------------


def _visibility(cdp_port: int, server_port: int) -> object:  # pragma: no cover - live
    return _evaluate(cdp_port, server_port, "document.visibilityState")


def _evaluate(cdp_port: int, server_port: int, expression: str) -> object:  # pragma: no cover
    from steerai.probes.layer_b import _ours  # noqa: PLC0415

    cdp, _ = _ours(cdp_port, server_port)
    try:
        reply = cdp.call("Runtime.evaluate", {"expression": expression, "returnByValue": True})
    finally:
        cdp.close()
    return reply.get("result", {}).get("value")


def _shown(hwnd: int, cdp_port: int, server_port: int) -> dict[str, object]:  # pragma: no cover
    """Raise our window; the 1-px move and back only if Chrome still holds the page hidden."""
    import ctypes  # noqa: PLC0415 - Windows only
    from ctypes import wintypes  # noqa: PLC0415

    from steerai.probes.win_input import acquire_foreground  # noqa: PLC0415

    acquire_foreground(hwnd)
    before = _visibility(cdp_port, server_port)
    nudged = False
    if before != "visible":
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            raise OSError("GetWindowRect failed")
        width, height = rect.right - rect.left, rect.bottom - rect.top
        # SWP_NOZORDER 0x4 | SWP_NOACTIVATE 0x10: our own window, one pixel and back
        user32.SetWindowPos(hwnd, None, rect.left + 1, rect.top, width, height, 0x14)
        time.sleep(0.2)
        user32.SetWindowPos(hwnd, None, rect.left, rect.top, width, height, 0x14)
        nudged = True
    deadline = time.monotonic() + 5.0
    after = _visibility(cdp_port, server_port)
    while after != "visible" and time.monotonic() < deadline:
        time.sleep(0.1)
        after = _visibility(cdp_port, server_port)
    time.sleep(0.5)
    return {"before": before, "nudged": nudged, "after": after}


def _page_value(cdp_port: int, server_port: int) -> object:  # pragma: no cover - live
    return _evaluate(cdp_port, server_port, f'document.getElementById("{ROW}").value')


def _g12_log(cdp_port: int, server_port: int) -> list[object]:  # pragma: no cover - live
    from steerai.probes.gallery_page import LOG_KEY  # noqa: PLC0415

    raw = _evaluate(cdp_port, server_port, f'localStorage.getItem("{LOG_KEY}") || "[]"')
    entries = json.loads(str(raw or "[]"))
    return [e for e in entries if isinstance(e, dict) and e.get("row") == ROW]


def _v1(hwnd: int) -> dict[str, object]:  # pragma: no cover - live
    import tempfile  # noqa: PLC0415

    from steerai.action.execution import UiaBindings  # noqa: PLC0415
    from steerai.action.executor import Executor  # noqa: PLC0415
    from steerai.action.live_reads import UiaLiveReads, twin_value_of  # noqa: PLC0415
    from steerai.action.options import UiaOptions  # noqa: PLC0415
    from steerai.action.physical import PhysicalExecutor  # noqa: PLC0415
    from steerai.orchestrator.contracts import OPTION_PARAM, Step, Target  # noqa: PLC0415
    from steerai.orchestrator.gate import Ask, Gate  # noqa: PLC0415
    from steerai.orchestrator.ledger import Ledger  # noqa: PLC0415
    from steerai.orchestrator.loop import Session  # noqa: PLC0415
    from steerai.orchestrator.trace import ListTrace  # noqa: PLC0415
    from steerai.probes.uia_snapshot import UiaClient  # noqa: PLC0415
    from steerai.world.model import WorldModel  # noqa: PLC0415

    class Person:
        def ask(self, ask: Ask) -> str:
            return f"yes:{ask.decision_id}"

    client = UiaClient()
    model = WorldModel(client)
    live = UiaLiveReads(client, UiaBindings.load())
    snapshot = model.observe(hwnd)
    combos = combo_rows(snapshot.elements, twin_value_of(snapshot, live))
    trace = ListTrace()
    session = Session(
        task_id="g12-probe",
        hwnd=hwnd,
        observer=model,
        semantic=Executor(options=UiaOptions(client)),  # as v1's run builds it
        physical=PhysicalExecutor(),
        gate=Gate(confirmer=Person(), own_pid=0, task_id="g12-probe"),
        ledger=Ledger(
            Path(tempfile.mkdtemp(prefix="steer-g12-")), task_id="g12-probe", started_at=time.time()
        ),
        live=live,
        trace=trace,
    )
    step = Step(
        id="s1",
        subgoal="g1",
        verb="choose",
        target=Target(role="ComboBox", name=LABEL),
        risk="R1",
        params={OPTION_PARAM: OPTION},
    )
    started = time.monotonic()
    try:
        result: dict[str, object] = step_record(session.run_step(step).to_dict())
    except Exception as exc:  # noqa: BLE001 - a probe records any failure as data
        result = {"raised": f"{type(exc).__name__}: {exc}"[:EVIDENCE_CUT]}
    return {
        "combos_before": combos,
        "result": result,
        "s": round(time.monotonic() - started, 3),
        "trace_events": [json.loads(line).get("event") for line in trace.lines][:40],
    }


def _v2(hwnd: int, pid: int) -> dict[str, object]:  # pragma: no cover - live
    from steerai.loop2.act_port import ActPort  # noqa: PLC0415
    from steerai.loop2.decide_port import TaskGenerations  # noqa: PLC0415
    from steerai.loop2.decision import ActionDoc  # noqa: PLC0415
    from steerai.loop2.live_world import (  # noqa: PLC0415
        LiveConfig,
        LiveWorld,
        WindowsPlatform,
        run_on_loop_thread,
    )
    from steerai.loop2.loop import Dispatch  # noqa: PLC0415
    from steerai.orchestrator.trace import ListTrace  # noqa: PLC0415
    from steerai.session.monitor import DEFAULT_KILL_KEY  # noqa: PLC0415

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
    platform = WindowsPlatform()

    def work() -> dict[str, object]:
        with LiveWorld(config, platform) as world:
            world.bring_to_front()
            observed = world.world.observe()
            elements = observed.snapshot.elements
            combos = combo_rows(elements, lambda e: _fact(world.world.read_value(e)))
            target, how = pick_combo(elements, LABEL)
            if target is None:
                return {"combos_before": combos, "picked": None}
            act = ActPort(
                world=world.world,
                semantic=world.semantic,
                physical=world.physical,
                desk=world.desk,
                interrupted=world.interrupted,
                clock=config.clock,
                wall_left=lambda: 60.0,
                pause=config.pause,
            )
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
                done = act.act(dispatch)
                result: dict[str, object] = {
                    "action": done.action.to_dict(),
                    "read_back": done.read_back,
                }
            except Exception as exc:  # noqa: BLE001 - a probe records any failure as data
                result = {"raised": f"{type(exc).__name__}: {exc}"[:EVIDENCE_CUT]}
            span = round(time.monotonic() - started, 3)
            after = _fact(world.world.read_value(target))
            return {
                "combos_before": combos,
                "picked": {"name": target.name, "how": how},
                "result": result,
                "s": span,
                "value_after": after,
            }

    return run_on_loop_thread(work, release=platform.release_com)


def _fact(fact: object) -> object:  # pragma: no cover - live
    """A ValueFact as plain data (its fields), or None."""
    if fact is None:
        return None
    return {"filled": getattr(fact, "filled", None), "digest": getattr(fact, "digest", None)}


def run_arm(
    arm: Arm, *, hwnd: int, pid: int, cdp_port: int, server_port: int
) -> int:  # pragma: no cover - live
    shown = _shown(hwnd, cdp_port, server_port)
    row: dict[str, object] = {"arm": arm, "shown": shown}
    row["page_value_before"] = _page_value(cdp_port, server_port)
    try:
        row.update(_v1(hwnd) if arm == "v1" else _v2(hwnd, pid))
    except Exception as exc:  # noqa: BLE001 - a probe records any failure as data
        row["raised"] = f"{type(exc).__name__}: {exc}"[:EVIDENCE_CUT]
    time.sleep(0.5)
    row["page_value_after"] = _page_value(cdp_port, server_port)
    row["g12_log"] = _g12_log(cdp_port, server_port)
    sys.stdout.write(json.dumps(row, default=str) + "\n")
    return 0


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
                done = subprocess.run(  # noqa: S603 - our own module, a fixed argv
                    [sys.executable, "-m", "steerai.probes.choose_probe", "arm",
                     "--arm", arm, "--hwnd", str(window.hwnd), "--pid", str(window.pid),
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
    parser = argparse.ArgumentParser(prog="python -m steerai.probes.choose_probe")
    sub = parser.add_subparsers(dest="mode", required=True)
    arm = sub.add_parser("arm")
    arm.add_argument("--arm", choices=("v1", "v2"), required=True)
    arm.add_argument("--hwnd", type=int, required=True)
    arm.add_argument("--pid", type=int, required=True)
    arm.add_argument("--cdp-port", type=int, required=True)
    arm.add_argument("--server-port", type=int, required=True)
    whole = sub.add_parser("run")
    whole.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        sys.stdout.write("choose_probe: windows only\n")
        return 0
    if args.mode == "arm":
        return run_arm(
            args.arm,
            hwnd=args.hwnd,
            pid=args.pid,
            cdp_port=args.cdp_port,
            server_port=args.server_port,
        )
    args.out_dir.mkdir(parents=True, exist_ok=True)
    return run(args.out_dir)


if __name__ == "__main__":  # pragma: no cover - live
    raise SystemExit(main())
