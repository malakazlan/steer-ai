"""[V2.57a] comparability (diagnosis only, probe-only): v1's own Session driving the gallery's
widget rows with its UIA client in a single-threaded apartment (v1's normal) and with the same
client forced into the MTA (the apartment v2's LiveWorld enters). Same steps, same page, fresh
throwaway Chrome per run; the page's own log says which events were trusted.

Each arm runs in a fresh child process (`arm`), so comtypes initialises once per apartment: the
`sta` arm builds everything on its main thread; the `mta` arm imports comtypes on its main thread
and runs the whole session on a thread that entered the MTA first. The steps send real input
(v1's executors, tagged); run only in a cleared slot.

Run: `python -m steerai.probes.mta_act run --runs 3 --out-dir D`.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final, Literal, get_args

Apartment = Literal["sta", "mta"]
APARTMENTS: Final[tuple[Apartment, ...]] = get_args(Apartment)
STEPS: Final[tuple[tuple[str, str, str, str, str | None], ...]] = (
    ("g02", "check", "Button", "Bold", None),
    ("g03", "check", "Button", "Dark mode", None),
    ("g04", "check", "CheckBox", "Email me updates", None),
    ("g07", "select", "RadioButton", "Express", None),
    ("g09", "select", "TabItem", "Billing details", None),
    ("g10", "select", "ListItem", "Mango", None),
    ("g12", "choose", "ComboBox", "Country", "Kenya"),
    ("g15", "expand", "Button", "Shipping policy", None),
    ("g16", "expand", "Button", "Show filters", None),
    ("g20", "expand", "TreeItem", "Projects", None),
    ("g21", "select", "DataItem", "Order 1002", None),
)
"""(gallery row, v1 verb, role, name, option): the widget rows a state verb or `choose` drives."""


def compare(rows: Sequence[Mapping[str, Any]]) -> dict[str, object]:  # Any: JSON
    """Per arm: runs, steps ok of steps tried, and the gallery rows whose logged event was
    trusted in every run; plus the rows that differ between the arms."""
    out: dict[str, object] = {}
    trusted: dict[str, set[str]] = {}
    for arm in APARTMENTS:
        mine = [r for r in rows if r.get("apartment") == arm]
        steps = [s for r in mine for s in r.get("steps", [])]
        sets = [set(r.get("trusted_rows", [])) for r in mine]
        trusted[arm] = set.intersection(*sets) if sets else set()
        out[arm] = {
            "runs": len(mine),
            "steps_ok": sum(1 for s in steps if s.get("ok")),
            "steps": len(steps),
            "trusted_in_every_run": sorted(trusted[arm]),
        }
    out["differ"] = sorted(trusted["sta"] ^ trusted["mta"])
    return out


def run_arm(
    apartment: Apartment, *, hwnd: int, cdp_port: int, server_port: int
) -> int:  # pragma: no cover
    """Every step of `STEPS` through v1's Session in `apartment`, then the page's own log."""
    import tempfile  # noqa: PLC0415
    import threading  # noqa: PLC0415

    import comtypes.client  # noqa: F401, PLC0415 - imported here, before any thread enters

    found: list[dict[str, object]] = []
    shown = _shown(hwnd, cdp_port, server_port)

    def body() -> None:
        if apartment == "mta":
            import ctypes  # noqa: PLC0415 - Windows only

            result = int(ctypes.windll.ole32.CoInitializeEx(None, 0)) & 0xFFFFFFFF
            if result not in (0, 1):
                raise OSError("not the MTA")
        found.append(_drive(hwnd, Path(tempfile.mkdtemp(prefix="steer-mta-act-"))))

    if apartment == "sta":
        body()
    else:
        thread = threading.Thread(target=body)
        thread.start()
        thread.join()
    row: dict[str, object] = found[0] if found else {"steps": [], "error": "no_result"}
    row["apartment"] = apartment
    row["shown"] = shown
    row["trusted_rows"] = _trusted_rows(cdp_port, server_port)
    sys.stdout.write(json.dumps(row) + "\n")
    return 0


def _visibility(cdp_port: int, server_port: int) -> object:  # pragma: no cover - live
    from steerai.probes.layer_b import _ours  # noqa: PLC0415

    cdp, _ = _ours(cdp_port, server_port)
    try:
        reply = cdp.call(
            "Runtime.evaluate", {"expression": "document.visibilityState", "returnByValue": True}
        )
    finally:
        cdp.close()
    return reply.get("result", {}).get("value")


def _shown(hwnd: int, cdp_port: int, server_port: int) -> dict[str, object]:  # pragma: no cover
    """Raise our window; if Chrome still holds the page hidden (the [V2.64] occlusion stall), the
    1-px move and back, then wait up to 5 s for `visible`. Both arms start from a shown page."""
    from steerai.probes.win_input import acquire_foreground  # noqa: PLC0415
    from steerai.probes.windows import get_window_rect, set_window_rect  # noqa: PLC0415

    acquire_foreground(hwnd)
    before = _visibility(cdp_port, server_port)
    nudged = False
    if before != "visible":
        left, top, right, bottom = get_window_rect(hwnd)
        set_window_rect(hwnd, (left + 1, top, right + 1, bottom))
        time.sleep(0.2)
        set_window_rect(hwnd, (left, top, right, bottom))
        nudged = True
    deadline = time.monotonic() + 5.0
    after = _visibility(cdp_port, server_port)
    while after != "visible" and time.monotonic() < deadline:
        time.sleep(0.1)
        after = _visibility(cdp_port, server_port)
    time.sleep(0.5)  # the rows re-exposed to UIA
    return {"before": before, "nudged": nudged, "after": after}


def _drive(hwnd: int, ledger_dir: Path) -> dict[str, object]:  # pragma: no cover - live
    from steerai.action.execution import UiaBindings  # noqa: PLC0415
    from steerai.action.executor import Executor  # noqa: PLC0415
    from steerai.action.live_reads import UiaLiveReads  # noqa: PLC0415
    from steerai.action.options import UiaOptions  # noqa: PLC0415
    from steerai.action.physical import PhysicalExecutor  # noqa: PLC0415
    from steerai.orchestrator.contracts import OPTION_PARAM, Step, Target  # noqa: PLC0415
    from steerai.orchestrator.gate import Ask, Gate  # noqa: PLC0415
    from steerai.orchestrator.ledger import Ledger  # noqa: PLC0415
    from steerai.orchestrator.loop import Session  # noqa: PLC0415
    from steerai.orchestrator.trace import ListTrace  # noqa: PLC0415
    from steerai.probes.uia_snapshot import UiaClient  # noqa: PLC0415
    from steerai.probes.win_input import acquire_foreground  # noqa: PLC0415
    from steerai.world.model import WorldModel  # noqa: PLC0415

    class Person:
        def ask(self, ask: Ask) -> str:
            return f"yes:{ask.decision_id}"

    acquire_foreground(hwnd)
    client = UiaClient()
    model = WorldModel(client)
    session = Session(
        task_id="mta-act",
        hwnd=hwnd,
        observer=model,
        semantic=Executor(options=UiaOptions(client)),  # as v1's run builds it (#406)
        physical=PhysicalExecutor(),
        gate=Gate(confirmer=Person(), own_pid=0, task_id="mta-act"),
        ledger=Ledger(ledger_dir, task_id="mta-act", started_at=time.time()),
        live=UiaLiveReads(client, UiaBindings.load()),
        trace=ListTrace(),
    )
    steps: list[dict[str, object]] = []
    for index, (row, verb, role, name, option) in enumerate(STEPS, start=1):
        step = Step(
            id=f"s{index}",
            subgoal="g1",
            verb=verb,
            target=Target(role=role, name=name),
            risk="R1",
            params={OPTION_PARAM: option} if option else {},
        )
        started = time.monotonic()
        try:
            result = session.run_step(step)
            ok, detail = bool(result.ok), str(result.to_dict().get("action", {}).get("detail", ""))
        except Exception as exc:  # noqa: BLE001 - a probe records any failure as data
            ok, detail = False, f"{type(exc).__name__}: {exc}"[:200]
        steps.append({"row": row, "ok": ok, "detail": detail,
                      "s": round(time.monotonic() - started, 3)})  # fmt: skip
        time.sleep(0.3)
    return {"steps": steps}


def _trusted_rows(cdp_port: int, server_port: int) -> list[str]:  # pragma: no cover - live
    """The gallery rows the page logged as trusted events (its own log over DevTools)."""
    from steerai.probes.gallery_page import LOG_KEY  # noqa: PLC0415
    from steerai.probes.layer_b import _ours  # noqa: PLC0415

    cdp, _ = _ours(cdp_port, server_port)
    try:
        reply = cdp.call(
            "Runtime.evaluate",
            {"expression": f'localStorage.getItem("{LOG_KEY}") || "[]"', "returnByValue": True},
        )
    finally:
        cdp.close()
    entries = json.loads(str(reply.get("result", {}).get("value") or "[]"))
    return sorted(
        {str(e.get("row")) for e in entries if isinstance(e, dict) and not e.get("untrusted")}
    )


def run(runs: int, out_dir: Path) -> int:  # pragma: no cover - live
    """`runs` runs per apartment, alternating, each on a fresh gallery page."""
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
    for number in range(1, runs + 1):
        for apartment in APARTMENTS:
            tmp = Path(tempfile.mkdtemp(prefix="steer-mta-act-"))
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
                    time.sleep(1.0)  # the gallery's rows exposed
                    done = subprocess.run(  # noqa: S603 - our own module, a fixed argv
                        [sys.executable, "-m", "steerai.probes.mta_act", "arm",
                         "--apartment", apartment, "--hwnd", str(window.hwnd),
                         "--cdp-port", str(port), "--server-port", str(server.port)],
                        capture_output=True, text=True, timeout=300, check=False,
                    )  # fmt: skip
                    lines = [ln for ln in done.stdout.splitlines() if ln.strip()]
                    row = json.loads(lines[-1]) if lines else {"apartment": apartment}
                    row["run"] = number
                    rows.append(row)
                    sys.stdout.write(json.dumps(row) + "\n")
                    sys.stdout.flush()
                finally:
                    stop_chrome(proc, tmp / "profile")
                    shutil.rmtree(tmp, ignore_errors=True)
    summary = compare(rows)
    (out_dir / "mta_act.json").write_text(json.dumps({"rows": rows, "summary": summary}))
    sys.stdout.write(json.dumps({"summary": summary}) + "\n")
    return 0


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - live
    parser = argparse.ArgumentParser(prog="python -m steerai.probes.mta_act")
    sub = parser.add_subparsers(dest="mode", required=True)
    arm = sub.add_parser("arm")
    arm.add_argument("--apartment", choices=APARTMENTS, required=True)
    arm.add_argument("--hwnd", type=int, required=True)
    arm.add_argument("--cdp-port", type=int, required=True)
    arm.add_argument("--server-port", type=int, required=True)
    whole = sub.add_parser("run")
    whole.add_argument("--runs", type=int, default=3)
    whole.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        sys.stdout.write("mta_act: windows only\n")
        return 0
    if args.mode == "arm":
        return run_arm(
            args.apartment, hwnd=args.hwnd, cdp_port=args.cdp_port, server_port=args.server_port
        )
    args.out_dir.mkdir(parents=True, exist_ok=True)
    return run(args.runs, args.out_dir)


if __name__ == "__main__":  # pragma: no cover - live
    raise SystemExit(main())
