"""The Phase 1 bar (section 12: "10 single-app tasks pass reliably"), measured live.

Ten tasks on real apps and sites: Notepad, Google, YouTube, Wikipedia, and the probe inbox
(served from memory). Each run starts on its app: a fresh Notepad, or a throwaway Chrome profile
at the site, never the person's own profile. Then it runs `steerai.run` in process with the
task's goal. The person is played by code: `go` to every plan, `yes` only to an ask whose text
is the goal's own words or whose named target (a text-less ask: a button, a box) is written
verbatim in the goal (casefolded, whitespace folded; #200, H4 packet), `no` to anything else.
An `open` is recorded, never launched, because the launcher would start the person's own Chrome.

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
from dataclasses import dataclass, field, replace
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
WORDPAD = Path(r"C:\Program Files\Windows NT\Accessories\wordpad.exe")
Check = Literal[
    "notepad_text",
    "app_text",
    "url_search",
    "h2_event",
    "h3_event",
    "h3_final",
    "h4_event",
    "h4_final",
    "calc_display",
    "file_text",
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
    path: str = ""
    """For `url_search`: the results page's exact path."""
    param: str = ""
    """For `url_search`: the query parameter that holds the words."""
    approve_targets: frozenset[str] = frozenset()
    """Exact button names the person says yes to on an ask that carries no text (a form's
    submit, a delete, a dialog's Save)."""
    title: str = ""
    """For `url_search`: the results page's exact title, measured (H3 review item 1); empty
    means the title is not checked."""


PAGE_SETS = ("h2", "h3", "h4")
"""The `start` values that mean a held-out page served from memory."""


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


def _url_search(  # noqa: PLR0913 - one task's fields
    task_id: str,
    site: str,
    start: str,
    words: str,
    *,
    path: str,
    param: str = "q",
    title: str = "",
) -> Task:
    return Task(
        task_id,
        f"search {site} for {words}",
        start,
        "url_search",
        words,
        frozenset({words}),
        path=path,
        param=param,
        title=title,
    )


CHALLENGE_TITLES = frozenset({"Client Challenge", "Just a moment..."})
"""Bot-check interstitials at a results URL (PyPI, Cloudflare): the run measured nothing."""


def _read_page(port: int, stage: str) -> str:
    """The page's href and title as JSON; a bot check is an invalid run, never a pass."""
    expression = "JSON.stringify({href: location.href, title: document.title})"
    observed = _read(port, expression, stage)
    if str(json.loads(observed or "{}").get("title", "")) in CHALLENGE_TITLES:
        raise InvalidRun("bot_check")
    return observed


HELD_OUT: tuple[Task, ...] = (
    Task(
        "hn1",
        "type meeting moved to 3pm, room 4 in notepad",
        "notepad",
        "notepad_text",
        "meeting moved to 3pm, room 4",
    ),
    Task(
        "wp1", "type quarterly plan draft in wordpad", "wordpad", "app_text", "quarterly plan draft"
    ),
    _url_search(
        "bing1", "Bing", "https://www.bing.com/?cc=us", "computer use agents", path="/search"
    ),
    _url_search("ddg1", "DuckDuckGo", "https://duckduckgo.com/", "ui automation tree", path="/"),
    _url_search("gh1", "GitHub", "https://github.com/", "computer use agent", path="/search"),
    _url_search(
        "py1", "the Python docs", "https://docs.python.org/3/", "pathlib", path="/3/search.html"
    ),
    _url_search(
        "mdn1", "MDN", "https://developer.mozilla.org/en-US/", "flexbox", path="/en-US/search"
    ),
    Task(
        "wk3",
        "open the Wikipedia article on Grace Hopper",
        "https://en.wikipedia.org/wiki/Main_Page",
        "wiki_article",
        "/wiki/Grace_Hopper",
        frozenset({"Grace Hopper"}),
    ),
    Task("ib3", "open the chat with Bilal", "inbox", "inbox_chat", "chat-bilal"),
    Task(
        "ib4",
        "reply see you tomorrow to Sara Ahmed",
        "inbox",
        "inbox_send",
        "chat-sara",
        frozenset({"see you tomorrow"}),
    ),
)
"""Ten other single-app tasks, never used to fix Steer when first run (design 12 [v2.43]). Spent
since: their traces were read to file #147-#151, so a rerun after those fixes is a development
result; Phase 1 is met only on a fresh held-out set (H2, [v2.45])."""


def results_url(task: Task) -> str:
    """The page a `url_search` task should end on, built from its own check (the positive
    control opens it directly, #152 review item 1), or a `wiki_article` task's article."""
    origin = urllib.parse.urlsplit(task.start)
    if task.check == "wiki_article":
        return urllib.parse.urlunsplit((origin.scheme, origin.netloc, task.expected, "", ""))
    query = urllib.parse.urlencode({task.param: task.expected})
    return urllib.parse.urlunsplit((origin.scheme, origin.netloc, task.path, query, ""))


CONTROL_ARGS = ("--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding")
"""A control's Chrome opens behind the person's windows and never takes the foreground (it
injects no OS input), and Chrome backgrounds a renderer whose window is occluded: measured on
the H4 page controls (2026-11-10, two diagnostic runs), `document.visibilityState` was `hidden`
and trusted CDP clicks and keys produced no events (`events:0`). Chrome's own switches keep the
renderer foregrounded while occluded (Chromium `content_switches.cc`)."""


def controls(tasks: Sequence[Task]) -> list[str]:
    """Each `url_search` task's own results URL (or a `wiki_article` task's article) opened
    directly in a throwaway Chrome, read the way a run is read, and judged by `passed`: a check
    that never returns true on real pages would fail every run silently. The read's title is
    printed too: five control runs are the n=5 title measurement."""
    lines = []
    for task in tasks:
        if task.check not in ("url_search", "wiki_article"):
            continue
        tmp = Path(tempfile.mkdtemp(prefix="steer-ctl-"))
        port = _free_port()
        proc = launch_chrome(
            CHROME_DEFAULT,
            tmp / "profile",
            port,
            results_url(task),
            window_size=WINDOW,
            extra_args=CONTROL_ARGS,
        )
        try:
            page_ws_url(port)
            _settled(port, SETTLE_LIMIT_S)
            time.sleep(4.0)  # the site settles its first layout and any redirect
            observed = _read_page(port, "control")
            ok = passed(task, "done", observed)
            page = json.loads(observed or "{}")
            # a public site's address and title: tokens, never person data
            read = json.dumps(page if not ok else str(page.get("title", "")))
            lines.append(f"{task.task_id}: control {'passes' if ok else 'FAILS'} title={read}")
        except InvalidRun as exc:
            lines.append(f"{task.task_id}: control invalid ({exc})")
        finally:
            from steerai.probes.chrome_a11y import stop_chrome  # noqa: PLC0415 - with launch

            stop_chrome(proc, tmp / "profile")
            shutil.rmtree(tmp, ignore_errors=True)
    return lines


SETTLE_LIMIT_S = 20.0


def _settled(port: int, limit_s: float) -> None:
    """The site's own document is loaded: an http page in readyState complete. The first read
    can land on the initial blank document, and a slow site (Gutenberg, H4 control run 4: an
    empty title) past a fixed sleep; a site that never loads is an invalid control, not a FAIL."""
    deadline = time.monotonic() + limit_s
    expression = "JSON.stringify({href: location.href, ready: document.readyState})"
    while True:
        page = json.loads(_read(port, expression, "settle") or "{}")
        if str(page.get("href", "")).startswith("http") and page.get("ready") == "complete":
            return
        if time.monotonic() >= deadline:
            raise InvalidRun("page_not_loaded")
        time.sleep(0.5)


MULTIPLY = chr(0xD7)
"""The multiplication sign Calculator writes in its expression."""
SAVED_TEXT = "steer h2 saved"
SAVED_NAME = "steer-h2.txt"


def _h2(task_id: str, goal: str, event: dict[str, Any], **kw: Any) -> Task:
    return Task(task_id, goal, "h2", "h2_event", json.dumps(event, sort_keys=True), **kw)


H2: tuple[Task, ...] = (
    _h2(
        "f1",
        "fill the contact form with name Amir Khan and email amir@example.com and send it",
        {"kind": "contact", "name": "Amir Khan", "email": "amir@example.com"},
        approve_targets=frozenset({"Send message"}),
    ),
    _h2("s1", "turn on dark mode in the settings", {"kind": "dark", "on": True}),
    _h2("c1", "choose Pakistan as the country", {"kind": "country", "value": "Pakistan"}),
    _h2(
        "t1",
        "add buy milk to the todo list",
        {"kind": "todo_add", "text": "buy milk"},
        approve=frozenset({"buy milk"}),
    ),
    _h2(
        "d1",
        "delete the task call mom",
        {"kind": "todo_delete", "text": "call mom"},
        approve_targets=frozenset({"Delete call mom"}),
    ),
    _h2("m1", "show the workspace's About information", {"kind": "about"}),
    _h2("b1", "open the Billing tab", {"kind": "tab", "name": "Billing"}),
    _h2("l1", "load more items in the feed", {"kind": "load_more"}),
    Task(
        "calc1",
        "calculate 12 times 7 in calculator",
        "calculator",
        "calc_display",
        json.dumps({"result": "84", "expression": f"12{MULTIPLY}7="}),
    ),
    Task(
        "save1",
        f"type {SAVED_TEXT} in notepad and save it as {SAVED_NAME} in the folder TMPDIR",
        "notepad_save",
        "file_text",
        SAVED_TEXT,
        frozenset({SAVED_TEXT, SAVED_NAME}),
        approve_targets=frozenset({"Save"}),
    ),
)
"""H2, the fresh held-out set (design 12 [v2.45]): committed before the #147-#151 fixes and run
only after them; eight widget shapes on one local page (a form, a checkbox, a select, a todo
add and an R3 delete, a menu, a tab, a button below the fold), a UWP app and a Save dialog.
`TMPDIR` in save1's goal is the run's own folder, filled in at run time."""


def _h3(task_id: str, goal: str, event: dict[str, Any], **kw: Any) -> Task:
    return Task(task_id, goal, "h3", "h3_event", json.dumps(event, sort_keys=True), **kw)


H3: tuple[Task, ...] = (
    _h3("n1", "turn on the weekly digest email", {"kind": "digest", "on": True}),
    Task(
        "n2",
        "turn on every notification type",
        "h3",
        "h3_final",
        json.dumps(
            {
                "kinds": ["notifications"],
                "state": {"Follows": True, "Mentions": True, "Replies": True},
                "untrusted": False,
            },
            sort_keys=True,
        ),
    ),
    _h3("n3", "set the shipping country to France", {"kind": "shipping", "value": "France"}),
    _h3("n4", "switch to the annual billing period", {"kind": "period", "value": "Annual"}),
    _h3("n5", "show the answers about shipping", {"kind": "faq", "name": "Shipping", "open": True}),
    _h3("n6", "show more comments in the discussion", {"kind": "more_comments"}),
    _h3("n7", "pick Kyoto from the destinations", {"kind": "destination", "value": "Kyoto"}),
    _url_search(
        "npm1",
        "npm",
        "https://www.npmjs.com/",
        "left-pad",
        path="/search",
        title="left-pad - npm search",
    ),
    _url_search(
        "cr1",
        "crates.io",
        "https://crates.io/",
        "serde",
        path="/search",
        title="Search Results for 'serde' - crates.io: Rust Package Registry",
    ),
    _url_search(
        "pypi1",
        "PyPI",
        "https://pypi.org/",
        "requests",
        path="/search/",
        title="Search results \u00b7 PyPI",
    ),
)
"""H3, the third held-out set (design 12 [v2.54]): written and committed before the widget-verb
and offscreen-lines fixes, run only after them. Seven widget shapes on one new local page
(`h3_page`; they share the page and move together), three real sites no earlier set used, each
judged on its measured results title too. Revised after the review of PR #175: n1 keeps its old
value until the 600 ms confirm, n2 is judged on the final state, n5 is an accordion, n6 a button
in a scrolling box, n7 a listbox below the fold; the near-copies (a tab, Calculator) are out."""


def _h4(task_id: str, goal: str, event: dict[str, Any], **kw: Any) -> Task:
    return Task(task_id, goal, "h4", "h4_event", json.dumps(event, sort_keys=True), **kw)


H4: tuple[Task, ...] = (
    _h4("q1", "expand the returns policy", {"kind": "policy", "open": True}),
    _h4("q2", "choose Swahili in the languages list", {"kind": "language", "value": "Swahili"}),
    Task(
        "q3",
        "set the country to Japan and the city to Osaka",
        "h4",
        "h4_final",
        json.dumps(
            {
                "kinds": ["city"],
                "state": {"city": "Osaka", "country": "Japan"},
                "untrusted": False,
            },
            sort_keys=True,
        ),
    ),
    _h4("q4", "open the Security tab", {"kind": "tab", "name": "Security"}),
    _h4(
        "q5",
        "tick the I agree to the terms box on the sign-up form",
        {"kind": "consent", "on": True},
    ),
    _url_search(
        "dr1",
        "docs.rs",
        "https://docs.rs/",
        "serde",
        path="/releases/search",
        param="query",
        title="Releases - Docs.rs",
    ),
    _url_search(
        "deb1",
        "Debian packages",
        "https://packages.debian.org/",
        "curl",
        path="/search",
        param="keywords",
        title="Debian -- Package Search Results -- curl",
    ),
    _url_search(
        "gut1",
        "Project Gutenberg",
        "https://www.gutenberg.org/",
        "dickens",
        path="/ebooks/search/",
        param="query",
        title="Books: dickens - Project Gutenberg",
    ),
    _url_search(
        "rg1",
        "RubyGems",
        "https://rubygems.org/",
        "rails",
        path="/search",
        param="query",
        title="RubyGems.org | your community gem host",
    ),
    Task(
        "aw1",
        "search the Arch Wiki for pacman",
        "https://wiki.archlinux.org/",
        "wiki_article",
        "/title/Pacman",
        frozenset({"pacman"}),
    ),
)
"""H4, the fourth held-out set (packet h4-2026-11-10): written and committed before the post-H3
fixes, run only after them. Five widget shapes on one new local page (`h4_page`: a native
disclosure, a list select scrolled inside its box, dependent selects judged on the final state,
anchor tabs, a consent box the gate asks about and the goal rule approves), four real search
sites no earlier set used, each judged on its measured results title too, and an ArchWiki
article reached by a MediaWiki "go", judged on its path."""


def h2_event(raw: str) -> str:
    """The one trusted event the page logged, as sorted JSON, or why not: a stray second
    event (a delete during an add) fails the run."""
    try:
        entries = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return "bad_log"
    if not isinstance(entries, list):
        return "bad_log"
    if len(entries) != 1:
        return f"events:{len(entries)}"
    entry = entries[0]
    if entry.get("untrusted"):
        return "untrusted"
    return json.dumps(entry, sort_keys=True)


CALC_IDS = ("CalculatorResults", "CalculatorExpression")
"""Calculator's own AutomationIds for the result and the expression (English names: "Display is
84", "Expression is 12 x 7="): the run must have computed 12 x 7, not typed 84."""


def calc_display(named: Sequence[tuple[str, str]]) -> str:
    """The result and the expression from (automation id, name) pairs, as sorted JSON."""
    found = dict(named)
    result = found.get("CalculatorResults", "").removeprefix("Display is ").strip()
    expression = found.get("CalculatorExpression", "").removeprefix("Expression is ")
    return json.dumps({"result": result, "expression": "".join(expression.split())}, sort_keys=True)


H2_DRIVES: dict[str, tuple[tuple[str, str], ...]] = {
    "f1": (
        ("type", "#f-name|Amir Khan"),
        ("type", "#f-email|amir@example.com"),
        ("click", "#f-send"),
    ),
    "s1": (("click", "#s-dark"),),
    "c1": (("focus", "#s-country"), ("char", "P")),
    "t1": (("type", "#t-new|buy milk"), ("key", "Enter")),
    "d1": (("click", 'button[data-del="call mom"]'),),
    "m1": (("click", "#menu-help"), ("click", "#help-about")),
    "b1": (("click", "#tab-billing"),),
    "l1": (("click", "#more"),),
}
"""What a person would do for each H2 page task, driven over CDP Input (trusted events) by the
positive control only."""


def _drive(cdp: CdpSession, drive: tuple[tuple[str, str], ...]) -> None:
    for kind, arg in drive:
        if kind == "click":
            target = f"document.querySelector({json.dumps(arg)})"
            script = (
                f"(function(){{var e={target}; e.scrollIntoView();"
                "var r=e.getBoundingClientRect();"
                "return JSON.stringify([r.x+r.width/2, r.y+r.height/2]);})()"
            )
            point = _evaluate(cdp, script, "point")
            x, y = json.loads(point)
            for event in ("mousePressed", "mouseReleased"):
                cdp.call(
                    "Input.dispatchMouseEvent",
                    {"type": event, "x": x, "y": y, "button": "left", "clickCount": 1},
                )
        elif kind == "focus":
            _evaluate(cdp, f"document.querySelector({json.dumps(arg)}).focus(); 'ok'", "focus")
        elif kind == "char":
            # rawKeyDown carries no text: a keyDown with text types it, and a char types it again
            for event in ("rawKeyDown", "char", "keyUp"):
                params = {"type": event, "key": arg, "code": f"Key{arg}"}
                if event == "char":
                    params["text"] = arg
                cdp.call("Input.dispatchKeyEvent", params)
        elif kind == "type":
            selector, text = arg.split("|", 1)
            _evaluate(cdp, f"document.querySelector({json.dumps(selector)}).focus(); 'ok'", "focus")
            cdp.call("Input.insertText", {"text": text})
        else:
            for event in ("keyDown", "keyUp"):
                cdp.call(
                    "Input.dispatchKeyEvent",
                    {"type": event, "key": arg, "code": arg, "windowsVirtualKeyCode": 13},
                )
        time.sleep(0.3)


H3_DRIVES: dict[str, tuple[tuple[str, str], ...]] = {
    "n1": (("click", "#e-digest"),),
    "n2": (("click", "#n-all"),),
    "n3": (("focus", "#a-ship"), ("char", "F")),
    "n4": (("click", "#p-year"),),
    "n5": (("click", "#faq-ship-btn"),),
    "n6": (("click", "#more-comments"),),
    "n7": (("click", "#d-kyoto"),),
}
"""What a person would do for each H3 page task, as trusted CDP input (positive control only)."""


def h3_controls() -> list[str]:
    """Each H3 page task done by scripted trusted input and judged by `passed`; the three
    search tasks by opening their results URL and checking the measured title."""
    return _page_controls(H3, H3_DRIVES) + controls(
        [task for task in H3 if task.check == "url_search"]
    )


H4_DRIVES: dict[str, tuple[tuple[str, str], ...]] = {
    "q1": (("click", "#policy-summary"),),
    "q2": (("click", "#l-swahili"),),
    "q3": (("focus", "#country"), ("char", "J"), ("focus", "#city"), ("char", "O")),
    "q4": (("click", "#t-security"),),
    "q5": (("click", "#consent"),),
}
"""What a person would do for each H4 page task, as trusted CDP input (positive control only):
q3 types the first letter of the country, then of the city it revealed."""


def h4_controls() -> list[str]:
    """Each H4 page task done by scripted trusted input and judged by `passed`; the four search
    tasks by opening their results URL and the ArchWiki task by opening its article."""
    return _page_controls(H4, H4_DRIVES) + controls(
        [task for task in H4 if task.check in ("url_search", "wiki_article")]
    )


def _page_controls(
    tasks: Sequence[Task], drives: dict[str, tuple[tuple[str, str], ...]]
) -> list[str]:
    lines = []
    for task in tasks:
        drive = drives.get(task.task_id)
        if drive is None:
            continue
        tmp = Path(tempfile.mkdtemp(prefix="steer-ctl-"))
        with PageServer({"/page.html": _page_for(task)}) as server:
            port = _free_port()
            url = f"http://127.0.0.1:{server.port}/page.html"
            proc = launch_chrome(
                CHROME_DEFAULT,
                tmp / "profile",
                port,
                url,
                window_size=WINDOW,
                extra_args=CONTROL_ARGS,
            )
            try:
                page_ws_url(port)
                _settled(port, SETTLE_LIMIT_S)
                time.sleep(2.0)
                cdp = _page(port)
                try:
                    _drive(cdp, drive)
                finally:
                    cdp.close()
                observed = _page_observed(task, port)
                ok = passed(task, "done", observed)
                # on a fail, the fixture's own token (events:0, untrusted, the page state) and
                # whether Chrome had the window as occluded (visibilityState hidden)
                seen = (
                    ""
                    if ok
                    else (
                        f" observed={observed} visibility="
                        f"{_read(port, 'document.visibilityState', 'visibility')}"
                    )
                )
                lines.append(f"{task.task_id}: control {'passes' if ok else 'FAILS'}{seen}")
            finally:
                from steerai.probes.chrome_a11y import stop_chrome  # noqa: PLC0415

                stop_chrome(proc, tmp / "profile")
                shutil.rmtree(tmp, ignore_errors=True)
    return lines


def h2_controls() -> list[str]:
    """Each H2 page task done by scripted trusted input, read the way a run is read and judged
    by `passed`; then Calculator computed by UIA Invoke on its own buttons (no OS input) and
    read the way calc1 is read."""
    lines = _page_controls(H2, H2_DRIVES)
    calc1 = next(task for task in H2 if task.task_id == "calc1")
    lines.append(
        _calc_control(
            calc1, ("num1Button", "num2Button", "multiplyButton", "num7Button", "equalButton")
        )
    )
    return lines


def _calc_control(task: Task, buttons: Sequence[str]) -> str:
    """The task's sum through Calculator's own buttons by InvokePattern, then its read."""
    from steerai.action.execution import UIA_INVOKE_PATTERN, UiaBindings  # noqa: PLC0415
    from steerai.probes.uia_snapshot import UiaClient  # noqa: PLC0415
    from steerai.probes.windows import list_top_windows  # noqa: PLC0415

    if any(w.title == "Calculator" for w in list_top_windows()):
        return f"{task.task_id}: control invalid (calculator_already_open)"
    subprocess.Popen([str(CALC)])  # noqa: S603 - a fixed system path
    deadline = time.monotonic() + 20.0
    window = None
    while window is None and time.monotonic() < deadline:
        window = next((w for w in list_top_windows() if w.title == "Calculator"), None)
        time.sleep(0.5)
    if window is None:
        return f"{task.task_id}: control invalid (calculator_not_shown)"
    try:
        time.sleep(2.0)
        bindings = UiaBindings.load()
        tree = UiaClient().cached_tree(window.hwnd)
        by_id = {n.automation_id: h for n, h in zip(tree.nodes, tree.handles, strict=True)}
        for button in buttons:
            bindings.pattern(by_id[button], UIA_INVOKE_PATTERN, "InvokePattern").Invoke()
            time.sleep(0.2)
        ok = passed(task, "done", calc_display(_calc_names(window.hwnd)))
        return f"{task.task_id}: control {'passes' if ok else 'FAILS'}"
    finally:
        _close(window.hwnd)


def _page_for(task: Task) -> str:
    """The held-out page a page task runs on (H2, H3 or H4)."""
    if task.start == "h4":
        from steerai.probes.h4_page import page_html as h4_html  # noqa: PLC0415 - H4 only

        return h4_html()
    if task.start == "h3":
        from steerai.probes.h3_page import page_html as h3_html  # noqa: PLC0415 - H3 only

        return h3_html()
    from steerai.probes.h2_page import page_html as h2_html  # noqa: PLC0415 - H2 only

    return h2_html()


PENDING_LIMIT_S = 5.0


def _page_observed(task: Task, port: int) -> str:
    """A page task's read, once the page's saves in flight are done (`window.PENDING`, H3 n1:
    the confirm lands 600 ms after the change; review item 3), capped at PENDING_LIMIT_S."""
    deadline = time.monotonic() + PENDING_LIMIT_S
    while time.monotonic() < deadline:
        if _read(port, "String(window.PENDING || 0)", "pending") == "0":
            break
        time.sleep(0.1)
    if task.check in ("h3_final", "h4_final"):
        return _read(port, "JSON.stringify(window.finalState())", "final")
    key = _page_log_key(task)
    return h2_event(_read(port, f'localStorage.getItem("{key}") || "[]"', "read"))


def _page_log_key(task: Task) -> str:
    if task.start == "h4":
        from steerai.probes.h4_page import LOG_KEY as H4_KEY  # noqa: PLC0415 - H4 only

        return H4_KEY
    if task.start == "h3":
        from steerai.probes.h3_page import LOG_KEY as H3_KEY  # noqa: PLC0415 - H3 only

        return H3_KEY
    from steerai.probes.h2_page import LOG_KEY as H2_KEY  # noqa: PLC0415 - H2 only

    return H2_KEY


def task_set(name: str) -> tuple[Task, ...]:
    if name == "h4":
        return H4
    if name == "h3":
        return H3
    if name == "h2":
        return H2
    return HELD_OUT if name == "held_out" else TASKS


def _folded(text: str) -> str:
    return " ".join(text.split()).casefold()


@dataclass
class Person:
    """`go` to every plan; `yes` only to an ask whose text is one of the task's exact phrases,
    or to a text-less ask whose target is a listed name or is written verbatim in the goal
    (#200: the goal "tick I agree to the terms" names the box the gate asks about)."""

    approve: frozenset[str]
    approve_targets: frozenset[str] = frozenset()
    goal: str = ""
    log: list[dict[str, Any]] = field(default_factory=list)

    def ask(self, ask: Any) -> str:  # Any: steerai.orchestrator.gate.Ask (no import cycle)
        decision = str(ask.decision_id)
        text = ask.params.get("text") if hasattr(ask, "params") else None
        wanted = {_folded(a) for a in self.approve}
        target = str(getattr(ask, "target_name", ""))
        in_goal = bool(target.strip()) and _folded(target) in _folded(self.goal)
        approved = getattr(ask, "kind", "yes") != "which" and (
            (isinstance(text, str) and _folded(text) in wanted)
            or (text is None and (target in self.approve_targets or in_goal))
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


def passed(task: Task, status: str | None, observed: str) -> bool:  # noqa: PLR0911 - one per check
    """Steer said `done` and the independent read agrees exactly (#139 review item 3)."""
    if status != "done":
        return False
    if task.check == "calc_display":
        return bool(json.loads(observed or "{}") == json.loads(task.expected))
    if task.check in ("h3_final", "h4_final"):
        return bool(json.loads(observed or "{}") == json.loads(task.expected))
    if task.check in ("notepad_text", "app_text", "h2_event", "h3_event", "h4_event"):
        return observed.strip() == task.expected
    if task.check == "file_text":
        return observed == task.expected
    if task.check in ("inbox_send", "inbox_chat"):
        return observed == task.expected
    page = json.loads(observed or "{}")
    href, title = str(page.get("href", "")), str(page.get("title", ""))
    path = urllib.parse.urlsplit(href).path
    if task.check == "url_search":
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query)
        words = " ".join(query.get(task.param, [""])[0].split())
        same_title = not task.title or title == task.title
        return path == task.path and words.casefold() == task.expected.casefold() and same_title
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


def seconds_line(rows: Sequence[dict[str, Any]], tasks: Sequence[Task] = TASKS) -> str:
    """p50 / p95 wall seconds per task over its measured runs."""
    parts = []
    for task in tasks:
        times = sorted(
            float(r["seconds"]) for r in rows if r.get("task") == task.task_id and "seconds" in r
        )
        if times:
            p50 = times[len(times) // 2]
            p95 = times[min(len(times) - 1, round(0.95 * (len(times) - 1)))]
            parts.append(f"{task.task_id} {p50:.1f}/{p95:.1f}")
    # with three runs a task's p95 is its slowest run
    return "seconds p50/p95: " + ", ".join(parts)


def verdict(
    rows: Sequence[dict[str, Any]], tasks: Sequence[Task] = TASKS
) -> tuple[list[str], bool]:
    """Every task of the set present and every run of it passed; an invalid run is reported and
    fails the bar too (it measured nothing, so the task is not proven)."""
    lines: list[str] = []
    ok = bool(rows)
    for task in tasks:
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


def notepad_text(hwnd: int, edit_class: str = "Edit") -> str:
    """An app's text through its edit child (WM_GETTEXT; classic Notepad's `Edit`, WordPad's
    `RICHEDIT50W`), read after the run."""
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
    edit = user32.FindWindowExW(hwnd, None, edit_class, None)
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
    person = Person(task.approve, task.approve_targets, goal=task.goal)
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


def _run_app(  # noqa: PLR0913 - one app's start and read, passed once
    task: Task, number: int, tmp: Path, planner: _Planner, *, exe: Path, edit_class: str
) -> dict[str, Any]:
    proc = subprocess.Popen([str(exe)])  # noqa: S603 - a fixed system path
    try:
        window = wait_window_for_pid(proc.pid, image=exe.name, process=proc)
        _in_front(window.hwnd)
        row = _steer(task, number, window.hwnd, tmp, planner)
        row["observed"] = notepad_text(window.hwnd, edit_class)
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
        elif task.check in ("h2_event", "h3_event", "h3_final", "h4_event", "h4_final"):
            row["observed"] = _page_observed(task, port)
        elif task.check == "inbox_chat":
            row["observed"] = _read(port, "document.body.dataset.chat", "chat")
        else:
            row["observed"] = _read_page(port, "page")
        return row
    finally:
        from steerai.probes.chrome_a11y import stop_chrome  # noqa: PLC0415 - with launch

        stop_chrome(proc, tmp / "profile")


def one_run(task: Task, number: int, planner: _Planner, keep: Path | None = None) -> dict[str, Any]:
    try:
        wait_until_idle()
    except InvalidRun as exc:
        # the person stayed active: this run measured nothing, the suite goes on
        return {"task": task.task_id, "run": number, "passed": False, "invalid": str(exc)}
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


def _close(hwnd: int) -> None:
    """WM_CLOSE to the one window this run opened; never a kill by image name, which could
    reach a Calculator the person had open."""
    user32: Any = ctypes.WinDLL("user32")  # Any: a ctypes library handle
    user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    user32.PostMessageW(hwnd, 0x0010, 0, 0)


CALC = Path(r"C:\Windows\System32\calc.exe")


def _calc_names(hwnd: int) -> list[tuple[str, str]]:
    """Raw UIA (the probe client's cached tree, not Steer's world model): the names of the
    result and expression elements by AutomationId."""
    from steerai.probes.uia_snapshot import UiaClient  # noqa: PLC0415 - Windows only

    tree = UiaClient().cached_tree(hwnd)
    return [(n.automation_id, n.name) for n in tree.nodes if n.automation_id in CALC_IDS]


def _run_calculator(task: Task, number: int, tmp: Path, planner: _Planner) -> dict[str, Any]:
    """Calculator is a UWP app behind ApplicationFrameHost: found as the new window titled
    exactly "Calculator"; a Calculator already open makes the run invalid (never the person's)."""
    from steerai.probes.windows import list_top_windows  # noqa: PLC0415 - Windows only

    if any(w.title == "Calculator" for w in list_top_windows()):
        raise InvalidRun("calculator_already_open")
    subprocess.Popen([str(CALC)])  # noqa: S603 - a fixed system path
    deadline = time.monotonic() + 20.0
    window = None
    while window is None and time.monotonic() < deadline:
        window = next((w for w in list_top_windows() if w.title == "Calculator"), None)
        time.sleep(0.5)
    if window is None:
        raise InvalidRun("calculator_not_shown")
    try:
        time.sleep(2.0)  # the UWP frame fills its content (unmeasured)
        _in_front(window.hwnd)
        row = _steer(task, number, window.hwnd, tmp, planner)
        row["observed"] = calc_display(_calc_names(window.hwnd))
        return row
    finally:
        _close(window.hwnd)


SAVE_ROOT = Path(Path(__file__).anchor) / "steer-probe"
"""Save into a folder on the repository's drive with no user name in its path: the goal goes to
the planner and any kept trace (#154 review item 4)."""
STRAY = Path.home() / "Documents" / SAVED_NAME
"""Where a Save that ignored the folder lands by default: a file there fails the run."""


def _run_notepad_save(task: Task, number: int, tmp: Path, planner: _Planner) -> dict[str, Any]:
    """Notepad, then its Save dialog: the saved file in the run's own folder is the read."""
    if STRAY.exists():
        raise InvalidRun("stray_file_exists")  # never the person's file: measure nothing
    folder = SAVE_ROOT / tmp.name
    folder.mkdir(parents=True)
    goal = task.goal.replace("TMPDIR", str(folder))
    proc = subprocess.Popen([str(NOTEPAD)])  # noqa: S603 - a fixed system path
    try:
        window = wait_window_for_pid(proc.pid, image="notepad.exe", process=proc)
        _in_front(window.hwnd)
        row = _steer(replace(task, goal=goal), number, window.hwnd, tmp, planner)
        saved = folder / SAVED_NAME
        if STRAY.exists():
            row["observed"] = "saved_to_documents"
        elif saved.is_file():
            row["observed"] = saved.read_text(encoding="utf-8-sig").strip()
        else:
            row["observed"] = "file_missing"
        return row
    finally:
        subprocess.run(  # noqa: S603 - fixed argv
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],  # noqa: S607 - taskkill on PATH
            check=False,
            capture_output=True,
        )
        shutil.rmtree(folder, ignore_errors=True)


def _started(  # noqa: PLR0911 - one per start kind
    task: Task, number: int, tmp: Path, planner: _Planner
) -> dict[str, Any]:
    if task.start == "notepad":
        return _run_app(task, number, tmp, planner, exe=NOTEPAD, edit_class="Edit")
    if task.start == "wordpad":
        if not WORDPAD.is_file():
            raise InvalidRun("wordpad_absent")  # removed from newer Windows builds
        return _run_app(task, number, tmp, planner, exe=WORDPAD, edit_class="RICHEDIT50W")
    if task.start in PAGE_SETS:
        with PageServer({"/page.html": _page_for(task)}) as server:
            return _run_chrome(
                task, number, tmp, planner, f"http://127.0.0.1:{server.port}/page.html"
            )
    if task.start == "calculator":
        return _run_calculator(task, number, tmp, planner)
    if task.start == "notepad_save":
        return _run_notepad_save(task, number, tmp, planner)
    if task.start == "inbox":
        reply = next(iter(task.approve)) if task.check == "inbox_send" else REPLY_TEXT
        with PageServer({"/page.html": page_html(reply)}) as server:
            return _run_chrome(
                task, number, tmp, planner, f"http://127.0.0.1:{server.port}/page.html"
            )
    return _run_chrome(task, number, tmp, planner, task.start)


def _header() -> dict[str, Any]:
    """Which code and which Chrome produced the rows (#139 review item 4)."""
    sha = subprocess.run(
        ["git", "describe", "--always", "--dirty"],  # noqa: S607 - git on PATH
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
    parser.add_argument("--set", choices=("dev", "held_out", "h2", "h3", "h4"), default="dev")
    parser.add_argument(
        "--controls", action="store_true", help="open each search task's results URL instead"
    )
    parser.add_argument("--tasks", default="", help="comma-separated task ids (default: all)")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--keep", type=Path, default=None, help="copy plans and traces here")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    wanted = set(filter(None, args.tasks.split(",")))
    tasks = task_set(args.set)
    if args.controls:
        if args.set == "h2":
            lines = h2_controls()
        elif args.set == "h3":
            lines = h3_controls()
        elif args.set == "h4":
            lines = h4_controls()
        else:
            lines = controls(tasks)
        sys.stdout.write("\n".join(lines) + "\n")
        return 0 if all(" control passes" in line for line in lines) else 1
    planner = _Planner()
    rows: list[dict[str, Any]] = []
    with args.out.open("w", encoding="utf-8") as out:
        out.write(json.dumps(_header()) + "\n")
        for number in range(1, args.runs + 1):
            for task in tasks:
                if wanted and task.task_id not in wanted:
                    continue
                row = one_run(task, number, planner, args.keep)
                rows.append(row)
                out.write(json.dumps(row) + "\n")
                out.flush()
    lines, ok = verdict(rows, tasks)
    lines.append(seconds_line(rows, tasks))
    sys.stdout.write("\n".join(lines) + f"\nbar {'met' if ok else 'missed'}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
