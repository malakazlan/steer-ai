"""The Phase 1 bar (section 12: "10 single-app tasks pass reliably"), measured live.

Ten tasks on real apps and sites: Notepad, Google, YouTube, Wikipedia, and the probe inbox
(served from memory). Each run starts on its app: a fresh Notepad, or a throwaway Chrome profile
at the site, never the person's own profile. Then it runs `steerai.run` in process with the
task's goal. The person is played by code: `go` to every plan, `yes` only to an ask whose text
is the goal's own words, `no` to anything else. An `open` is recorded, never launched, because
the launcher would start the person's own Chrome.

Each result is checked independently of Steer's own report: Notepad's text through
WM_GETTEXT, the page address over CDP, the inbox page's own log. Code checks the bar
(`verdict`). Prints tokens and counts only. Never in CI: it injects input, calls models and
reaches the network.

Usage (Windows, the person away: the idle gate waits before every run):
    uv run python -m steerai.probes.task_suite --runs 3 --out suite.jsonl
"""

from __future__ import annotations

import argparse
import ctypes
import io
import json
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from collections.abc import Sequence
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from steerai import run as run_mod
from steerai.planner.contracts import PlanOutcome, PlanRequest
from steerai.probes.chrome_a11y import CHROME_DEFAULT, CdpSession, launch_chrome, page_ws_url
from steerai.probes.layer_b import (
    WINDOW,
    _evaluate,
    _free_port,
    _in_front,
    _recorded_opens,
    wait_until_idle,
)
from steerai.probes.layer_b_pages import OPEN_GOAL, REPLY_GOAL, InvalidRun, PageServer
from steerai.probes.present_switch import _plan_kinds, _steps, page_html
from steerai.probes.windows import wait_window_for_pid

NOTEPAD = Path(r"C:\Windows\System32\notepad.exe")
Check = Literal["notepad_text", "url_path", "url_query", "inbox_send", "inbox_chat"]


@dataclass(frozen=True, slots=True)
class Task:
    task_id: str
    goal: str
    start: str
    """`notepad`, `inbox`, or the https URL the throwaway Chrome opens on."""
    check: Check
    expected: str
    """The Notepad text, the page path prefix, the search words, or the inbox chat id."""


TASKS: tuple[Task, ...] = (
    Task("np1", "type hello from steer in notepad", "notepad", "notepad_text", "hello from steer"),
    Task(
        "np2",
        "type the quick brown fox in notepad",
        "notepad",
        "notepad_text",
        "the quick brown fox",
    ),
    Task(
        "g1",
        "search Google for steer ai computer use agent",
        "https://www.google.com/?hl=en",
        "url_query",
        "steer ai computer use agent",
    ),
    Task(
        "g2",
        "search Google for windows ui automation",
        "https://www.google.com/?hl=en",
        "url_query",
        "windows ui automation",
    ),
    Task(
        "yt1",
        "search YouTube for lofi hip hop",
        "https://www.youtube.com/",
        "url_query",
        "lofi hip hop",
    ),
    Task(
        "yt2",
        "search YouTube for drake latest video and play the first video",
        "https://www.youtube.com/",
        "url_path",
        "/watch",
    ),
    Task(
        "wk1",
        "search Wikipedia for Alan Turing",
        "https://en.wikipedia.org/wiki/Main_Page",
        "url_path",
        "/wiki/Alan_Turing",
    ),
    Task(
        "wk2",
        "search Wikipedia for Ada Lovelace",
        "https://en.wikipedia.org/wiki/Main_Page",
        "url_path",
        "/wiki/Ada_Lovelace",
    ),
    Task("ib1", REPLY_GOAL, "inbox", "inbox_send", "chat-amir-khan"),
    Task("ib2", OPEN_GOAL, "inbox", "inbox_chat", "chat-sara"),
)


@dataclass
class Person:
    """`go` to every plan; `yes` only to an ask whose text is the goal's own words."""

    goal: str
    log: list[dict[str, Any]] = field(default_factory=list)

    def ask(self, ask: Any) -> str:  # Any: steerai.orchestrator.gate.Ask (no import cycle)
        decision = str(ask.decision_id)
        text = ask.params.get("text") if hasattr(ask, "params") else None
        approved = (
            getattr(ask, "kind", "yes") != "which"
            and isinstance(text, str)
            and bool(text.strip())
            and text.strip().casefold() in self.goal.casefold()
        )
        self.log.append({"verb": str(ask.verb), "risk": str(ask.risk), "approved": approved})
        return f"{'yes' if approved else 'no'}:{decision}"

    def ask_go(self, go_id: str) -> str:  # noqa: ARG002 - the protocol's shape
        return "go"


def query_of(url: str) -> str:
    """The search words a results page carries (`q` or `search_query`), or empty."""
    params = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    for key in ("q", "search_query"):
        if params.get(key):
            return " ".join(params[key][0].split())
    return ""


def passed(task: Task, status: str | None, observed: str) -> bool:
    """Steer said `done` and the independent read agrees."""
    if status != "done":
        return False
    if task.check == "notepad_text":
        return observed.strip() == task.expected
    if task.check == "url_query":
        return query_of(observed).casefold() == task.expected.casefold()
    if task.check == "url_path":
        return urllib.parse.urlsplit(observed).path.startswith(task.expected)
    return observed == task.expected


def verdict(rows: Sequence[dict[str, Any]]) -> tuple[list[str], bool]:
    """Every task present and every run of it passed; an invalid run is reported and fails the
    bar too (it measured nothing, so the task is not proven)."""
    lines: list[str] = []
    ok = bool(rows)
    for task in TASKS:
        runs = [r for r in rows if r["task"] == task.task_id]
        good = sum(bool(r["passed"]) for r in runs)
        invalid = sum("invalid" in r for r in runs)
        lines.append(
            f"{task.task_id}: {good}/{len(runs)}" + (f" ({invalid} invalid)" if invalid else "")
        )
        ok = ok and bool(runs) and good == len(runs)
    total = sum(bool(r["passed"]) for r in rows)
    lines.append(f"all: {total}/{len(rows)}")
    return lines, ok


@dataclass
class _Planner:
    calls: int = 0

    def __call__(self, request: PlanRequest) -> PlanOutcome:
        self.calls += 1
        return run_mod._planner(request)


_WM_GETTEXT = 0x000D
_WM_GETTEXTLENGTH = 0x000E


def notepad_text(hwnd: int) -> str:
    """Classic Notepad's text through its Edit child (WM_GETTEXT), read after the run."""
    user32: Any = ctypes.WinDLL("user32")  # Any: a ctypes library handle
    user32.FindWindowExW.restype = wintypes.HWND
    user32.FindWindowExW.argtypes = (
        wintypes.HWND,
        wintypes.HWND,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
    )
    user32.SendMessageW.restype = ctypes.c_ssize_t
    user32.SendMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    edit = user32.FindWindowExW(hwnd, None, "Edit", None)
    if not edit:
        return ""
    length = int(user32.SendMessageW(edit, _WM_GETTEXTLENGTH, 0, 0))
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.SendMessageW(edit, _WM_GETTEXT, length + 1, ctypes.addressof(buffer))
    return buffer.value


def _options(tmp: Path, hwnd: int, task: Task, number: int) -> run_mod.Options:
    return run_mod.Options(
        plan=tmp / "task.plan.json",
        window=None,
        hwnd=hwnd,
        task_id=f"suite-{task.task_id}-{number}-{secrets.token_hex(4)}",
        ledger_dir=tmp / "ledger",
        trace=tmp / "trace.jsonl",
        max_risk="R3",
        max_steps=12,
        verify_deadline_s=3.0,
        warmup_s=5.0,
        allow_plan_change=False,
        allow_window_change=False,
        goal=task.goal,
    )


def _steer(task: Task, number: int, hwnd: int, tmp: Path, planner: _Planner) -> dict[str, Any]:
    person = Person(task.goal)
    deps = run_mod._real_deps().replace(
        make_confirmer=lambda *_: person,
        open_windows=lambda: [],
        planner=planner,
        plan_cache=None,  # never the person's cache, never a replayed plan
    )
    stdout = io.StringIO()
    started = time.monotonic()
    with _recorded_opens() as opens:
        run_mod.run(
            _options(tmp, hwnd, task, number),
            deps,
            stdin=io.StringIO(""),
            stdout=stdout,
            stderr=io.StringIO(),
        )
        opened = len(opens)
    payload = stdout.getvalue().strip()
    result = json.loads(payload) if payload.startswith("{") else {}
    trace = tmp / "trace.jsonl"
    records = (
        [json.loads(x) for x in trace.read_text("utf-8").splitlines()] if trace.is_file() else []
    )
    return {
        "expects": _plan_kinds(tmp),
        "steps": _steps(records),
        "status": result.get("status"),
        "detail": result.get("detail"),
        "seconds": round(time.monotonic() - started, 1),
        "asks": "".join("y" if a["approved"] else "n" for a in person.log),
        "opens_recorded": opened,
    }


def _run_notepad(task: Task, number: int, tmp: Path, planner: _Planner) -> dict[str, Any]:
    proc = subprocess.Popen([str(NOTEPAD)])  # noqa: S603 - a fixed system path
    try:
        window = wait_window_for_pid(proc.pid, image="notepad.exe", process=proc)
        _in_front(window.hwnd)
        row = _steer(task, number, window.hwnd, tmp, planner)
        row["observed"] = notepad_text(window.hwnd)
        return row
    finally:
        subprocess.run(  # noqa: S603 - fixed argv
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],  # noqa: S607 - taskkill on PATH
            check=False,
            capture_output=True,
        )


def _href(port: int) -> str:
    cdp = CdpSession(page_ws_url(port))
    try:
        return _evaluate(cdp, "location.href", "href")
    finally:
        cdp.close()


def _run_chrome(task: Task, number: int, tmp: Path, planner: _Planner, url: str) -> dict[str, Any]:
    port = _free_port()
    proc = launch_chrome(CHROME_DEFAULT, tmp / "profile", port, url, window_size=WINDOW)
    try:
        window = wait_window_for_pid(proc.pid, image="chrome.exe", process=proc)
        _href(port)  # the page target exists before Steer looks
        time.sleep(3.0)  # a real site settles its first layout (unmeasured; the warmup follows)
        _in_front(window.hwnd)
        row = _steer(task, number, window.hwnd, tmp, planner)
        if task.check == "inbox_send":
            cdp = CdpSession(page_ws_url(port))
            raw = _evaluate(cdp, 'localStorage.getItem("steer-present") || "[]"', "read")
            cdp.close()
            sends = [
                e.get("chat") for e in json.loads(raw) if e.get("trusted") and e.get("expected")
            ]
            row["observed"] = sends[0] if len(sends) == 1 else f"sends:{len(sends)}"
        elif task.check == "inbox_chat":
            cdp = CdpSession(page_ws_url(port))
            row["observed"] = _evaluate(cdp, "document.body.dataset.chat", "chat")
            cdp.close()
        else:
            row["observed"] = _href(port)
        return row
    finally:
        from steerai.probes.chrome_a11y import stop_chrome  # noqa: PLC0415 - with launch

        stop_chrome(proc, tmp / "profile")


def one_run(task: Task, number: int, planner: _Planner, keep: Path | None = None) -> dict[str, Any]:
    wait_until_idle()
    tmp = Path(tempfile.mkdtemp(prefix="steer-suite-"))
    try:
        row = _started(task, number, tmp, planner)
        if keep is not None:
            # diagnosis only: the plans and trace of a fixture goal, into a local folder
            for kept in [*tmp.glob("*.plan*.json"), tmp / "trace.jsonl"]:
                if kept.is_file():
                    shutil.copy(kept, keep / f"{task.task_id}-{number}-{kept.name}")
    except InvalidRun as exc:
        # the run measured nothing (the window never came to front): reported, never a pass
        return {"task": task.task_id, "run": number, "passed": False, "invalid": str(exc)}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    observed = str(row.pop("observed", ""))
    ok = passed(task, row["status"], observed) and row["opens_recorded"] == 0
    # the observed address is a public site's, the text a fixture's: tokens, never person data
    return {
        "task": task.task_id,
        "run": number,
        "passed": ok,
        **row,
        "planner_calls": planner.calls,
    }


def _started(task: Task, number: int, tmp: Path, planner: _Planner) -> dict[str, Any]:
    if task.start == "notepad":
        return _run_notepad(task, number, tmp, planner)
    if task.start == "inbox":
        with PageServer({"/page.html": page_html()}) as server:
            return _run_chrome(
                task, number, tmp, planner, f"http://127.0.0.1:{server.port}/page.html"
            )
    return _run_chrome(task, number, tmp, planner, task.start)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 1 bar: ten single-app tasks, live.")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--tasks", default="", help="comma-separated task ids (default: all)")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--keep", type=Path, default=None, help="copy plans and traces here")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    wanted = set(filter(None, args.tasks.split(",")))
    planner = _Planner()
    rows: list[dict[str, Any]] = []
    with args.out.open("w", encoding="utf-8") as out:
        for number in range(1, args.runs + 1):
            for task in TASKS:
                if wanted and task.task_id not in wanted:
                    continue
                row = one_run(task, number, planner, args.keep)
                rows.append(row)
                out.write(json.dumps(row) + "\n")
                out.flush()
    lines, ok = verdict(rows)
    sys.stdout.write("\n".join(lines) + f"\nbar {'met' if ok else 'missed'}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
