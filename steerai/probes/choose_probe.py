"""Gallery row g12 (`<select>` "Country", choose "Kenya"), diagnosis only, probe-only: why v1's
Session failed it in every arm of the [V2.57a] apartment comparison, and whether v2's act port
fails it too.

One throwaway Chrome on the gallery page. Arm `v1`: the ComboBoxes as UIA names them and their
live values, then the one step through v1's own Session (its executor built as v1's run builds
it, with the option source: without one a choose refuses `no_option_source`, 2026-11-15), its
whole result kept (gate outcome, codes, the action and the evidence). The page is reloaded.
Arm `v2` (the v2 package's own `choose_probe`, which also runs both arms: v1's side never
imports the v2 package): the same read, then the same single step through the act port a
`LiveWorld` hands the loop (no model call), its `ActResult` and the value read after. Each arm
runs in its own child process (one COM apartment each) and first shows the page (raise; the
1-px nudge only if Chrome holds it hidden). The page's own log gives the g12 events after each
arm. Sends real input to our own window only.

Run through the v2 package's `choose_probe` (`run --out-dir D`); this module's `arm` is its v1
child.
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


def fact_data(fact: object) -> object:  # pragma: no cover - live
    """A ValueFact as plain data (its fields), or None."""
    if fact is None:
        return None
    return {"filled": getattr(fact, "filled", None), "digest": getattr(fact, "digest", None)}


def arm_row(
    arm: Arm,
    body: Callable[[], dict[str, object]],
    *,
    hwnd: int,
    cdp_port: int,
    server_port: int,
) -> int:  # pragma: no cover - live
    """One arm's line: the page shown, its value, the arm's `body`, its value and log after."""
    shown = _shown(hwnd, cdp_port, server_port)
    row: dict[str, object] = {"arm": arm, "shown": shown}
    row["page_value_before"] = _page_value(cdp_port, server_port)
    try:
        row.update(body())
    except Exception as exc:  # noqa: BLE001 - a probe records any failure as data
        row["raised"] = f"{type(exc).__name__}: {exc}"[:EVIDENCE_CUT]
    time.sleep(0.5)
    row["page_value_after"] = _page_value(cdp_port, server_port)
    row["g12_log"] = _g12_log(cdp_port, server_port)
    sys.stdout.write(json.dumps(row, default=str) + "\n")
    return 0


def add_arm_args(arm: argparse.ArgumentParser) -> None:  # pragma: no cover - live
    arm.add_argument("--hwnd", type=int, required=True)
    arm.add_argument("--pid", type=int, required=True)
    arm.add_argument("--cdp-port", type=int, required=True)
    arm.add_argument("--server-port", type=int, required=True)


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - live
    parser = argparse.ArgumentParser(prog="python -m steerai.probes.choose_probe")
    sub = parser.add_subparsers(dest="mode", required=True)
    add_arm_args(sub.add_parser("arm"))
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        sys.stdout.write("choose_probe: windows only\n")
        return 0
    return arm_row(
        "v1",
        lambda: _v1(args.hwnd),
        hwnd=args.hwnd,
        cdp_port=args.cdp_port,
        server_port=args.server_port,
    )


if __name__ == "__main__":  # pragma: no cover - live
    raise SystemExit(main())
