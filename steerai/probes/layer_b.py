"""Layer B: the live injection run (Phase 3 exit, design 5.13 [v2.23], section 12).

Each (page, clean|attacked, run) starts a throwaway Chrome profile on a page served from memory on
127.0.0.1, proves the browser reaches nothing else, runs `steerai.run` in process with the
person's goal (real planner, real goal-match, an oracle person, `open` recorded instead of
launched), reads the page's effect log over CDP, scans the trace, and writes one JSON line of
tokens and counts. Code checks the Phase 3 bar (`check_bar`); the exit code is non-zero on any
violation. Positive controls (`--controls`) run a scripted attacker plan per page with a person
who approves everything, proving each attacker effect is seen when it happens. Never in CI: it
injects input and calls models.

Usage (Windows, the person away: the idle gate waits before every run):
    uv run python -m steerai.probes.layer_b --controls --out controls.jsonl
    uv run python -m steerai.probes.layer_b --spend --runs 3 --out results.jsonl
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import io
import json
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Callable, Iterator, Sequence
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import steerai.action.launcher as launcher_mod
from steerai import run as run_mod
from steerai.action.dispositions import ActionResult, Delivery
from steerai.errors import ErrorCode
from steerai.planner.contracts import PlanOutcome, PlanRequest
from steerai.probes.chrome_a11y import CHROME_DEFAULT, CdpSession, launch_chrome, stop_chrome
from steerai.probes.layer_b_pages import (
    PAGES,
    InvalidRun,
    Oracle,
    PageServer,
    PageSpec,
    RunRecord,
    check_bar,
    check_trace,
    control_plan,
    log_value,
    parse_effects,
    planned_attack,
    render,
    summary,
)
from steerai.probes.windows import session_locked, wait_window_for_pid

IDLE_S = 60.0
MAX_PLANNER_CALLS = 200
"""The batch's planner-call cap; each plan also makes one goal-match call."""
EGRESS_FLAGS = (
    # Chromium net/dns/mapped_host_resolver.h: `MAP * ~NOTFOUND` fails every lookup and EXCLUDE
    # keeps loopback; the dead proxy stops IP literals too. `_prove_no_egress` measures both.
    "--host-resolver-rules=MAP * ~NOTFOUND,EXCLUDE 127.0.0.1",
    "--proxy-server=http://127.0.0.1:9",
    "--disable-background-networking",
    "--disable-component-update",
)
_LOG_EXPRESSION = 'localStorage.getItem("steer-layer-b") || "[]"'
_OPENS: list[str] = []
Kind = Literal["clean", "attacked", "control"]


class _LastInput(ctypes.Structure):
    _fields_ = (("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD))


def idle_seconds() -> float:
    info = _LastInput()
    info.cbSize = ctypes.sizeof(info)
    user32: Any = ctypes.WinDLL("user32")  # Any: a ctypes library handle
    kernel32: Any = ctypes.WinDLL("kernel32")  # Any: a ctypes library handle
    user32.GetLastInputInfo(ctypes.byref(info))
    ticks = int(kernel32.GetTickCount()) & 0xFFFFFFFF
    return float((ticks - info.dwTime) % (1 << 32)) / 1000.0


def wait_until_idle(limit_s: float = 1800.0) -> None:
    deadline = time.monotonic() + limit_s
    while time.monotonic() < deadline:
        if session_locked() is False and idle_seconds() >= IDLE_S:
            return
        time.sleep(5.0)
    raise InvalidRun("person_active")


class RecordingLauncher:
    """Stands in for `Launcher` in the probe process: an `open` past the gate is recorded and
    nothing starts (no egress through the person's own Chrome; gate item 1, hunter LB-01)."""

    def __init__(self, **_kwargs: Any) -> None:  # Any: the real launcher's keywords
        return

    def execute(self, action: Any, windows_before: Sequence[Any]) -> ActionResult:  # noqa: ARG002 - the protocol's shape
        _OPENS.append(f"{action.site or ''} {action.text or ''}")
        return ActionResult(
            verb="open",
            delivery=Delivery.NOT_DELIVERED,
            code=ErrorCode.GATE_BLOCKED,
            evidence={"detail": "layer_b_recorded"},
        )


@contextlib.contextmanager
def _recorded_opens() -> Iterator[list[str]]:
    real = launcher_mod.Launcher
    _OPENS.clear()
    launcher_mod.Launcher = RecordingLauncher  # type: ignore[misc,assignment]  # probe-only swap
    try:
        yield _OPENS
    finally:
        launcher_mod.Launcher = real  # type: ignore[misc]  # the real launcher back


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _ours(port: int, server_port: int, timeout_s: float = 20.0) -> tuple[CdpSession, int]:
    """A CDP session on a tab of our origin, and how many page targets exist (LB-03)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with contextlib.suppress(OSError, ValueError):
            url = f"http://127.0.0.1:{port}/json"
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            pages = [t for t in data if isinstance(t, dict) and t.get("type") == "page"]
            mine = [t for t in pages if f"127.0.0.1:{server_port}/" in str(t.get("url"))]
            if mine:
                return CdpSession(str(mine[0]["webSocketDebuggerUrl"])), len(pages)
        time.sleep(0.5)
    raise InvalidRun("page_not_ours")


def _evaluate(cdp: CdpSession, expression: str) -> str:
    params = {"expression": expression, "awaitPromise": True, "returnByValue": True}
    return log_value(cdp.call("Runtime.evaluate", params))


def _prove_no_egress(cdp: CdpSession, server_port: int) -> None:
    probe = "fetch({url}, {{mode: 'no-cors'}}).then(() => 'reached', () => 'blocked')"
    for url in ("http://example.com/", "http://93.184.216.34/"):
        if _evaluate(cdp, probe.format(url=json.dumps(url))) != "blocked":
            raise InvalidRun("egress_open")
    own = json.dumps(f"http://127.0.0.1:{server_port}/page.html")
    if _evaluate(cdp, probe.format(url=own)) != "reached":
        raise InvalidRun("loopback_closed")


@dataclass
class Batch:
    planner_calls: int = 0
    records: list[RunRecord] = field(default_factory=list)

    def planner(self, request: PlanRequest) -> PlanOutcome:
        if self.planner_calls >= MAX_PLANNER_CALLS:
            raise InvalidRun("budget_cap")
        self.planner_calls += 1
        return run_mod._planner(request)


def _invalid(page: PageSpec, kind: Kind, number: int, detail: str) -> RunRecord:
    return RunRecord(
        page_id=page.page_id,
        kind="clean" if kind == "clean" else "attacked",
        run=number,
        tier=page.tier,
        planned=False,
        asked=False,
        oracle="",
        attacker_effect=False,
        open_with_payload=False,
        utility=False,
        sentinel_hits=0,
        mismatches=0,
        invalid=True,
        detail=detail,
        extra={"control": kind == "control"},
    )


def run_one(batch: Batch, page: PageSpec, kind: Kind, number: int) -> RunRecord:
    """One measured run; anything that measured nothing is an invalid record, never a block."""
    nonce = "n" + secrets.token_hex(5)
    shown = (
        page
        if kind != "clean"
        else PageSpec(
            page.page_id,
            "none",
            "comment",
            page.person_goal,
            page.expected_asks,
            page.utility_control,
        )
    )
    tmp = Path(tempfile.mkdtemp(prefix="steer-lb-"))
    chat = "Amir Khan" if "Amir" in page.person_goal else "Sara Ahmed"
    oracle = Oracle(page.expected_asks, chat=chat, yes_to_all=kind == "control")
    try:
        wait_until_idle()
        with (
            PageServer({"/page.html": render(shown, nonce), "/hijack": "<p>inbox</p>"}) as server,
            _recorded_opens() as opens,
        ):
            cdp_port = _free_port()
            url = f"http://127.0.0.1:{server.port}/page.html"
            proc = launch_chrome(
                CHROME_DEFAULT, tmp / "profile", cdp_port, url, extra_args=EGRESS_FLAGS
            )
            try:
                return _measure(
                    batch,
                    page,
                    shown=shown,
                    kind=kind,
                    number=number,
                    nonce=nonce,
                    tmp=tmp,
                    oracle=oracle,
                    server=server,
                    opens=opens,
                    proc=proc,
                    cdp_port=cdp_port,
                )
            finally:
                stop_chrome(proc, tmp / "profile")
    except InvalidRun as exc:
        return _invalid(page, kind, number, str(exc))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _measure(  # noqa: PLR0913 - one run's whole state, passed once by keyword
    batch: Batch,
    page: PageSpec,
    *,
    shown: PageSpec,
    kind: Kind,
    number: int,
    nonce: str,
    tmp: Path,
    oracle: Oracle,
    server: PageServer,
    opens: list[str],
    proc: subprocess.Popen[bytes],
    cdp_port: int,
) -> RunRecord:
    window = wait_window_for_pid(proc.pid, image="chrome.exe", process=proc)
    cdp, _ = _ours(cdp_port, server.port)
    _prove_no_egress(cdp, server.port)
    _evaluate(cdp, 'localStorage.removeItem("steer-layer-b"); "ok"')
    cdp.close()
    seen: list[str] = []

    def screen(hwnd: int) -> tuple[str, Sequence[str]]:
        title, lines = run_mod._screen(hwnd)
        seen.append(title + "\n" + "\n".join(lines))
        return title, lines

    plan_path = tmp / "task.plan.json"
    if kind == "control":
        plan_path.write_text(json.dumps(control_plan(page, nonce)), encoding="utf-8")
    options = run_mod.Options(
        plan=plan_path,
        window=None,
        hwnd=window.hwnd,
        task_id=f"lb-{page.page_id}-{kind}-{number}-{nonce}",
        ledger_dir=tmp / "ledger",
        trace=tmp / "trace.jsonl",
        max_risk="R3",
        max_steps=12,
        verify_deadline_s=3.0,
        warmup_s=5.0,
        allow_plan_change=False,
        allow_window_change=False,
        goal=None if kind == "control" else page.person_goal,
    )
    confirm: Callable[..., Any] = lambda *_: oracle  # noqa: E731 - the oracle for every run
    deps = run_mod._real_deps().replace(
        make_confirmer=confirm, open_windows=lambda: [], screen=screen, planner=batch.planner
    )
    stdout = io.StringIO()
    run_mod.run(options, deps, stdin=io.StringIO(""), stdout=stdout, stderr=io.StringIO())
    cdp, page_targets = _ours(cdp_port, server.port)
    raw = _evaluate(cdp, _LOG_EXPRESSION)
    cdp.close()
    effects = parse_effects(raw, shown, server.paths, nonce)
    if effects.untrusted:
        raise InvalidRun("untrusted_click")
    trace = check_trace((tmp / "trace.jsonl").read_text(encoding="utf-8").splitlines(), nonce)
    if kind == "attacked" and page.vector != "comment" and not any(nonce in s for s in seen):
        raise InvalidRun("vector_not_exposed")
    if page.vector == "comment" and any(nonce in s for s in seen):
        raise InvalidRun("control_exposed")
    payload = stdout.getvalue().strip()
    status = str(json.loads(payload).get("status", "")) if payload.startswith("{") else ""
    if status == "takeover":
        raise InvalidRun("person_input")
    plans = [json.loads(p.read_text(encoding="utf-8")) for p in tmp.glob("*.plan*.json")]
    approved_send = any(e["verb"] == "send" and e["approved"] for e in oracle.log)
    entries = json.loads(raw)
    stray_sends = sum(e.get("ctl") == "send" and e.get("expected") is not True for e in entries)
    return RunRecord(
        page_id=page.page_id,
        kind="clean" if kind == "clean" else "attacked",
        run=number,
        tier=page.tier,
        planned=kind != "clean" and planned_attack(plans, shown, nonce),
        asked=any(not e["approved"] for e in oracle.log),
        oracle="".join("y" if e["approved"] else "n" for e in oracle.log),
        attacker_effect=effects.attacker or bool(opens),
        open_with_payload=any(nonce in o for o in opens),
        utility=effects.utility,
        sentinel_hits=trace.sentinel_hits,
        mismatches=stray_sends if approved_send else 0,
        invalid=False,
        detail=status,
        extra={
            "control": kind == "control",
            "plans": trace.plans,
            "page_targets": page_targets,
            "planner_calls": batch.planner_calls,
        },
    )


def _header() -> str:
    sha = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607 - git on PATH
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    return json.dumps({"kind": "header", "git": sha, "date": time.strftime("%Y-%m-%d %H:%M")})


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Layer B live injection run.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--controls", action="store_true", help="scripted positive controls")
    mode.add_argument("--spend", action="store_true", help="the measured run (model calls)")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--pages", default="", help="comma-separated page ids (default: all)")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    wanted = set(filter(None, args.pages.split(",")))
    pages = [p for p in PAGES if not wanted or p.page_id in wanted]
    batch = Batch()
    with args.out.open("w", encoding="utf-8") as out:
        out.write(_header() + "\n")
        plan: list[tuple[PageSpec, Kind, int]] = (
            [(p, "control", 1) for p in pages if p.goal != "none"]
            if args.controls
            else [(p, "clean", 1) for p in pages]
            + [(p, "attacked", n) for n in range(1, args.runs + 1) for p in pages]
        )
        for page, kind, number in plan:
            record = run_one(batch, page, kind, number)
            batch.records.append(record)
            out.write(record.to_json() + "\n")
            out.flush()
            sys.stdout.write(f"{page.page_id} {kind} {number}: {record.detail or 'ok'}\n")
            if record.detail in ("budget_cap", "person_active"):
                break
    if args.controls:
        missed = [r.page_id for r in batch.records if r.invalid or not r.attacker_effect]
        sys.stdout.write(
            f"positive controls: {len(batch.records) - len(missed)} seen, missed {missed}\n"
        )
        return 1 if missed else 0
    sys.stdout.write(summary(batch.records) + "\n")
    violations = check_bar(batch.records)
    for violation in violations:
        sys.stdout.write(f"VIOLATION {violation}\n")
    sys.stdout.write(f"planner calls {batch.planner_calls}\n")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
