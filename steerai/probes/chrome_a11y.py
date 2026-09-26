"""Phase 0 probe: Chromium accessibility behaviour under UIA.

Measures, on a throwaway Chrome profile showing a local test page:
  1. cold start: first UIA snapshot latency and how long the tree takes to stabilise
  2. after user input with no accessibility calls (Chromium's AutoDisableAccessibility rule:
     3+ input events over 30 s): does the tree shrink, and how long until it is back

Usage (Windows):
    uv run python -m steerai.probes.chrome_a11y --idle-seconds 35 --events 5 [--real-input]
"""

from __future__ import annotations

import argparse
import contextlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from steerai.probes.uia_snapshot import UiaClient
from steerai.probes.windows import WindowInfo, list_top_windows

CHROME_DEFAULT = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
_POLL_S = 0.25
_STABLE_WINDOW = 3


# ---------------------------------------------------------------- pure helpers


def build_test_page(*, links: int, buttons: int) -> str:
    items = [f'<a href="#a{i}">link {i}</a>' for i in range(links)]
    items += [f"<button>button {i}</button>" for i in range(buttons)]
    body = "\n".join(f"<li>{item}</li>" for item in items)
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>Steer a11y probe</title></head><body>"
        f"<main><h1>Steer a11y probe</h1><ul>{body}</ul></main></body></html>"
    )


def first_stable_index(counts: Sequence[int], *, window: int) -> int | None:
    """Index of the first reading that starts `window` equal consecutive readings."""
    if window < 2:
        raise ValueError("window must be >= 2")
    for start in range(len(counts) - window + 1):
        chunk = counts[start : start + window]
        if all(value == chunk[0] for value in chunk):
            return start
    return None


@dataclass(frozen=True, slots=True)
class CdpMessage:
    id: int | None = None
    method: str | None = None
    params: dict[str, Any] = field(default_factory=dict)  # JSON object
    result: dict[str, Any] | None = None  # JSON object

    def to_json(self) -> str:
        payload: dict[str, Any] = {"id": self.id, "method": self.method, "params": self.params}
        return json.dumps(payload)


def parse_cdp_message(raw: str) -> CdpMessage:
    data = json.loads(raw)
    return CdpMessage(
        id=data.get("id"),
        method=data.get("method"),
        params=data.get("params") or {},
        result=data.get("result"),
    )


# ---------------------------------------------------------------- CDP client


class CdpSession:
    """Minimal synchronous DevTools client bound to one page target."""

    def __init__(self, ws_url: str) -> None:
        # Lazy import: websockets is only needed by live probes.
        from websockets.sync.client import connect  # noqa: PLC0415

        self._ws = connect(ws_url, max_size=None, legacy=True)
        self._next_id = 1

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        message_id = self._next_id
        self._next_id += 1
        self._ws.send(CdpMessage(id=message_id, method=method, params=params or {}).to_json())
        while True:
            reply = parse_cdp_message(str(self._ws.recv(timeout=10)))
            if reply.id == message_id:
                return reply.result or {}

    def close(self) -> None:
        self._ws.close()


def page_ws_url(port: int, *, timeout_s: float = 15.0) -> str:
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2) as resp:
                targets = json.loads(resp.read().decode("utf-8"))
            for target in targets:
                if target.get("type") == "page":
                    return str(target["webSocketDebuggerUrl"])
        except (OSError, ValueError, KeyError) as exc:
            last_error = exc
        time.sleep(0.2)
    raise TimeoutError(f"no CDP page target on port {port}: {last_error}")


# ---------------------------------------------------------------- Chrome


def launch_chrome(chrome: Path, profile_dir: Path, port: int, url: str) -> subprocess.Popen[bytes]:
    args = [
        str(chrome),
        f"--user-data-dir={profile_dir}",
        f"--remote-debugging-port={port}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--window-size=1200,900",
        "--window-position=40,40",
        url,
    ]
    return subprocess.Popen(args)  # noqa: S603 - fixed argv built from validated paths


def window_for_pid(pid: int, *, timeout_s: float = 15.0) -> WindowInfo:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for window in list_top_windows():
            if window.pid == pid and "Google Chrome" in window.title:
                return window
        time.sleep(0.2)
    raise TimeoutError(f"no visible Chrome window for pid {pid}")


# ---------------------------------------------------------------- probe


@dataclass(frozen=True, slots=True)
class Reading:
    t_s: float
    node_count: int
    elapsed_ms: float


@dataclass(slots=True)
class PhaseResult:
    name: str
    readings: list[Reading] = field(default_factory=list)

    @property
    def first(self) -> Reading:
        return self.readings[0]

    @property
    def stable_at_s(self) -> float | None:
        index = first_stable_index([r.node_count for r in self.readings], window=_STABLE_WINDOW)
        return None if index is None else self.readings[index].t_s

    @property
    def final_count(self) -> int:
        return self.readings[-1].node_count


def poll_until_stable(
    client: UiaClient, hwnd: int, *, max_s: float, poll_s: float = _POLL_S
) -> PhaseResult:
    phase = PhaseResult(name="")
    started = time.monotonic()
    counts: list[int] = []
    while time.monotonic() - started < max_s:
        snapshot = client.snapshot_subtree(hwnd)
        phase.readings.append(
            Reading(
                t_s=round(time.monotonic() - started, 3),
                node_count=snapshot.node_count,
                elapsed_ms=round(snapshot.elapsed_ms, 1),
            )
        )
        counts.append(snapshot.node_count)
        if first_stable_index(counts, window=_STABLE_WINDOW) is not None:
            break
        time.sleep(poll_s)
    return phase


def dispatch_cdp_clicks(session: CdpSession, *, events: int, over_s: float) -> None:
    gap = over_s / events
    for _ in range(events):
        for kind in ("mousePressed", "mouseReleased"):
            session.call(
                "Input.dispatchMouseEvent",
                {"type": kind, "x": 300, "y": 300, "button": "left", "clickCount": 1},
            )
        time.sleep(gap)


def dispatch_real_clicks(window: WindowInfo, *, events: int, over_s: float) -> None:
    # Lazy import: Windows-only input module.
    from steerai.probes.win_input import click_at, pid_at_point, window_rect  # noqa: PLC0415

    left, top, _right, _bottom = window_rect(window.hwnd)
    x, y = left + 300, top + 300
    gap = over_s / events
    for _ in range(events):
        owner = pid_at_point(x, y)
        if owner != window.pid:
            raise RuntimeError(
                f"refusing to click: pixel ({x},{y}) is owned by pid {owner}, not {window.pid}"
            )
        click_at(x, y)
        time.sleep(gap)


@dataclass(frozen=True, slots=True)
class ProbeArgs:
    chrome: Path
    idle_seconds: float
    events: int
    real_input: bool
    out: str | None


def parse_args(argv: Sequence[str]) -> ProbeArgs:
    parser = argparse.ArgumentParser(description="Chromium accessibility behaviour under UIA.")
    parser.add_argument("--chrome", type=Path, default=CHROME_DEFAULT)
    parser.add_argument("--idle-seconds", type=float, default=35.0)
    parser.add_argument("--events", type=int, default=5)
    parser.add_argument("--real-input", action="store_true", help="also click with SendInput")
    parser.add_argument("--out", default=None)
    ns = parser.parse_args(argv)
    return ProbeArgs(
        chrome=ns.chrome,
        idle_seconds=ns.idle_seconds,
        events=ns.events,
        real_input=ns.real_input,
        out=ns.out,
    )


def run_probe(args: ProbeArgs) -> dict[str, Any]:
    if not args.chrome.is_file():
        raise FileNotFoundError(args.chrome)
    port = 9333
    profile_dir = Path(tempfile.mkdtemp(prefix="steer-a11y-"))
    page = profile_dir / "probe.html"
    page.write_text(build_test_page(links=300, buttons=60), encoding="utf-8")
    proc = launch_chrome(args.chrome, profile_dir, port, page.as_uri())
    result: dict[str, Any] = {"chrome": str(args.chrome), "events": args.events}
    try:
        session = CdpSession(page_ws_url(port))
        result["chrome_version"] = session.call("Browser.getVersion").get("product")
        window = window_for_pid(proc.pid)
        result["window"] = asdict(window)
        client = UiaClient()

        cold = poll_until_stable(client, window.hwnd, max_s=20)
        cold.name = "cold"
        result["cold"] = _phase_dict(cold)

        dispatch_cdp_clicks(session, events=args.events, over_s=args.idle_seconds)
        after_cdp = poll_until_stable(client, window.hwnd, max_s=20)
        after_cdp.name = "after_cdp_input"
        result["after_cdp_input"] = _phase_dict(after_cdp)

        if args.real_input:
            dispatch_real_clicks(window, events=args.events, over_s=args.idle_seconds)
            after_real = poll_until_stable(client, window.hwnd, max_s=20)
            after_real.name = "after_real_input"
            result["after_real_input"] = _phase_dict(after_real)
        session.close()
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)
        shutil.rmtree(profile_dir, ignore_errors=True)
    return result


def _phase_dict(phase: PhaseResult) -> dict[str, Any]:
    return {
        "first_query_ms": phase.first.elapsed_ms,
        "first_node_count": phase.first.node_count,
        "stable_at_s": phase.stable_at_s,
        "final_node_count": phase.final_count,
        "readings": [asdict(r) for r in phase.readings],
    }


def format_report(result: dict[str, Any]) -> str:
    lines = [f"chrome {result.get('chrome_version')}  events={result.get('events')}"]
    for name in ("cold", "after_cdp_input", "after_real_input"):
        phase = result.get(name)
        if phase is None:
            continue
        lines.append(
            f"{name:<18} first query {phase['first_query_ms']:>7.1f} ms, "
            f"first count {phase['first_node_count']:>5}, stable at {phase['stable_at_s']} s, "
            f"final count {phase['final_node_count']}"
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    result = run_probe(args)
    sys.stdout.write(format_report(result) + "\n")
    if args.out is not None:
        Path(args.out).write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
