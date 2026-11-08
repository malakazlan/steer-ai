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
import urllib.request
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
from steerai.probes.layer_b_pages import (
    OPEN_GOAL,
    REPLY_GOAL,
    REPLY_TEXT,
    InvalidRun,
    PageServer,
)
from steerai.probes.present_switch import _plan_kinds, _steps, page_html
from steerai.probes.windows import wait_window_for_pid

NOTEPAD = Path(r"C:\Windows\System32\notepad.exe")
Check = Literal[
    "notepad_text",
    "google_search",
    "youtube_search",
    "youtube_watch",
    "wiki_article",
    "inbox_send",
    "inbox_chat",
]


@dataclass(frozen=True, slots=True)
class Task:
    task_id: str
    goal: str
    start: str
    """`notepad`, `inbox`, or the https URL the throwaway Chrome opens on."""
    check: Check
    expected: str
    """The Notepad text, the search words, the word a watch page's title holds, the article
    path, or the inbox chat id."""
    approve: frozenset[str] = frozenset()
    """The exact texts the scripted person says yes to (casefolded equality, never a substring:
    "ok" is not "ok thanks", #139 review item 2)."""


def _search(task_id: str, site: str, start: str, words: str, check: Check) -> Task:
    return Task(task_id, f"search {site} for {words}", start, check, words, frozenset({words}))


TASKS: tuple[Task, ...] = (
    Task("np1", "type hello from steer in notepad", "notepad", "notepad_text", "hello from steer"),
    Task(
        "np2",
        "type the quick brown fox in notepad",
        "notepad",
        "notepad_text",
        "the quick brown fox",
    ),
    _search(
        "g1",
        "Google",
        "https://www.google.com/?hl=en",
        "steer ai computer use agent",
        "google_search",
    ),
    _search(
        "g2", "Google", "https://www.google.com/?hl=en", "windows ui automation", "google_search"
    ),
    _search("yt1", "YouTube", "https://www.youtube.com/", "lofi hip hop", "youtube_search"),
    Task(
        "yt2",
        "search YouTube for drake latest video and play the first video",
        "https://www.youtube.com/",
        "youtube_watch",
        "drake",
        frozenset({"drake latest video"}),
    ),
    Task(
        "wk1",
        "search Wikipedia for Alan Turing",
        "https://en.wikipedia.org/wiki/Main_Page",
        "wiki_article",
        "/wiki/Alan_Turing",
        frozenset({"Alan Turing"}),
    ),
    Task(
        "wk2",
        "search Wikipedia for Ada Lovelace",
        "https://en.wikipedia.org/wiki/Main_Page",
        "wiki_article",
        "/wiki/Ada_Lovelace",
        frozenset({"Ada Lovelace"}),
    ),
    Task("ib1", REPLY_GOAL, "inbox", "inbox_send", "chat-amir-khan", frozenset({REPLY_TEXT})),
    Task("ib2", OPEN_GOAL, "inbox", "inbox_chat", "chat-sara"),
)
"""Five task shapes, each twice: type in Notepad, search Google, search or play on YouTube,
open a Wikipedia article, reply or open a chat in the probe inbox. These were the development
set for #131/#133/#134/#137 (design 12 [v2.43]): a pass here is not a held-out result."""


@dataclass
class Person:
    """`go` to every plan; `yes` only to an ask whose text is one of the task's exact phrases."""

    approve: frozenset[str]
    log: list[dict[str, Any]] = field(default_factory=list)

    def ask(self, ask: Any) -> str:  # Any: steerai.orchestrator.gate.Ask (no import cycle)
        decision = str(ask.decision_id)
        text = ask.params.get("text") if hasattr(ask, "params") else None
        wanted = {" ".join(a.split()).casefold() for a in self.approve}
        approved = (
            getattr(ask, "kind", "yes") != "which"
            and isinstance(text, str)
            and " ".join(text.split()).casefold() in wanted
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


_SEARCH_PATHS: dict[str, str] = {"google_search": "/search", "youtube_search": "/results"}


def passed(task: Task, status: str | None, observed: str) -> bool:
    """Steer said `done` and the independent read agrees exactly (#139 review item 3)."""
    if status != "done":
        return False
    if task.check == "notepad_text":
        return observed.strip() == task.expected
    if task.check in ("inbox_send", "inbox_chat"):
        return observed == task.expected
    page = json.loads(observed or "{}")
    href, title = str(page.get("href", "")), str(page.get("title", ""))
    path = urllib.parse.urlsplit(href).path
    if task.check in _SEARCH_PATHS:
        return path == _SEARCH_PATHS[task.check] and (
            query_of(href).casefold() == task.expected.casefold()
        )
    if task.check == "youtube_watch":
        # "the first video" for the words: a watch page whose title names the artist
        return path == "/watch" and task.expected.casefold() in title.casefold()
    return path == task.expected


def inbox_send(raw: str) -> str:
    """The chat of the one send in the inbox log, or why not: a stray send before or after the
    right one fails the run (#139 review item 1)."""
    entries = json.loads(raw or "[]")
    if not isinstance(entries, list):
        raise ValueError("the send log is not a list")
    if len(entries) != 1:
        return f"sends:{len(entries)}"
    entry = entries[0]
    if not (entry.get("trusted") and entry.get("expected")):
        return "bad_send"
    return str(entry.get("chat"))


def seconds_line(rows: Sequence[dict[str, Any]]) -> str:
    """p50 / p95 wall seconds per task over its measured runs."""
    parts = []
    for task in TASKS:
        times = sorted(
            float(r["seconds"]) for r in rows if r.get("task") == task.task_id and "seconds" in r
        )
        if times:
            p50 = times[len(times) // 2]
            p95 = times[min(len(times) - 1, round(0.95 * (len(times) - 1)))]
            parts.append(f"{task.task_id} {p50:.1f}/{p95:.1f}")
    return "seconds p50/p95: " + ", ".join(parts)


def verdict(rows: Sequence[dict[str, Any]]) -> tuple[list[str], bool]:
    """Every task present and every run of it passed; an invalid run is reported and fails the
    bar too (it measured nothing, so the task is not proven)."""
    lines: list[str] = []
    ok = bool(rows)
    for task in TASKS:
        runs = [r for r in rows if r.get("task") == task.task_id]
        good = sum(bool(r["passed"]) for r in runs)
        invalid = sum("invalid" in r for r in runs)
        lines.append(
            f"{task.task_id}: {good}/{len(runs)}" + (f" ({invalid} invalid)" if invalid else "")
        )
        ok = ok and bool(runs) and good == len(runs)
    measured = [r for r in rows if "task" in r]
    total = sum(bool(r["passed"]) for r in measured)
    lines.append(f"all: {total}/{len(measured)}")
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
    person = Person(task.approve)
    deps = run_mod._real_deps().replace(
        make_confirmer=lambda *_: person,
        open_windows=lambda: [],
        planner=planner,
        plan_cache=None,  # never the person's cache, never a replayed plan
    )
    stdout = io.StringIO()
    started = time.monotonic()
    calls_before = planner.calls
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
        "planner_calls": planner.calls - calls_before,
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


def _page(port: int) -> CdpSession:
    """The one page tab: with more than one, a read could be of a tab Steer never used, so the
    run measured nothing (#139 review item 3)."""
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=5) as resp:
        targets = json.loads(resp.read().decode("utf-8"))
    pages = [t for t in targets if isinstance(t, dict) and t.get("type") == "page"]
    if len(pages) != 1:
        raise InvalidRun(f"page_targets:{len(pages)}")
    return CdpSession(str(pages[0]["webSocketDebuggerUrl"]))


def _read(port: int, expression: str, stage: str) -> str:
    cdp = _page(port)
    try:
        return _evaluate(cdp, expression, stage)
    finally:
        cdp.close()


def _run_chrome(task: Task, number: int, tmp: Path, planner: _Planner, url: str) -> dict[str, Any]:
    port = _free_port()
    proc = launch_chrome(CHROME_DEFAULT, tmp / "profile", port, url, window_size=WINDOW)
    try:
        window = wait_window_for_pid(proc.pid, image="chrome.exe", process=proc)
        page_ws_url(port)  # the page target exists before Steer looks
        time.sleep(3.0)  # a real site settles its first layout (unmeasured; the warmup follows)
        _in_front(window.hwnd)
        row = _steer(task, number, window.hwnd, tmp, planner)
        if task.check == "inbox_send":
            raw = _read(port, 'localStorage.getItem("steer-present") || "[]"', "read")
            row["observed"] = inbox_send(raw)
        elif task.check == "inbox_chat":
            row["observed"] = _read(port, "document.body.dataset.chat", "chat")
        else:
            expression = "JSON.stringify({href: location.href, title: document.title})"
            row["observed"] = _read(port, expression, "page")
        return row
    finally:
        from steerai.probes.chrome_a11y import stop_chrome  # noqa: PLC0415 - with launch

        stop_chrome(proc, tmp / "profile")


def one_run(task: Task, number: int, planner: _Planner, keep: Path | None = None) -> dict[str, Any]:
    try:
        wait_until_idle()
    except TimeoutError:
        # the person stayed active: this run measured nothing, the suite goes on
        return {"task": task.task_id, "run": number, "passed": False, "invalid": "person_active"}
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
    # a takeover is a fail, never invalid: conservative, labelled by its status token
    return {"task": task.task_id, "run": number, "passed": ok, **row}


def _started(task: Task, number: int, tmp: Path, planner: _Planner) -> dict[str, Any]:
    if task.start == "notepad":
        return _run_notepad(task, number, tmp, planner)
    if task.start == "inbox":
        with PageServer({"/page.html": page_html()}) as server:
            return _run_chrome(
                task, number, tmp, planner, f"http://127.0.0.1:{server.port}/page.html"
            )
    return _run_chrome(task, number, tmp, planner, task.start)


def _header() -> dict[str, Any]:
    """Which code and which Chrome produced the rows (#139 review item 4)."""
    sha = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607 - git on PATH
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    # Chrome installs into a folder named by its version next to chrome.exe
    versions = sorted(
        d.name
        for d in CHROME_DEFAULT.parent.iterdir()
        if d.is_dir() and d.name[:1].isdigit() and d.name.count(".") == 3
    )
    version = versions[-1] if versions else "unknown"
    return {
        "kind": "header",
        "git": sha,
        "chrome": version,
        "date": time.strftime("%Y-%m-%d %H:%M"),
    }


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
        out.write(json.dumps(_header()) + "\n")
        for number in range(1, args.runs + 1):
            for task in TASKS:
                if wanted and task.task_id not in wanted:
                    continue
                row = one_run(task, number, planner, args.keep)
                rows.append(row)
                out.write(json.dumps(row) + "\n")
                out.flush()
    lines, ok = verdict(rows)
    lines.append(seconds_line(rows))
    sys.stdout.write("\n".join(lines) + f"\nbar {'met' if ok else 'missed'}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
