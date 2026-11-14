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
from collections.abc import Callable, Mapping, Sequence
from ctypes import wintypes
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final, Literal

from steerai import run as run_mod
from steerai.debug_trace import TraceLevel
from steerai.orchestrator.plan_io import PlanError
from steerai.planner.contracts import PlannerError, PlanOutcome, PlanRequest
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
from steerai.probes.memory_guard import (
    EXIT_LOW_MEMORY,
    FLOOR_MB,
    LowMemory,
    claim_browser,
    free_physical_mb,
    preflight,
    refusal_row,
    release_browser,
)
from steerai.probes.present_switch import _plan_kinds, _steps, page_html
from steerai.probes.suite_chunks import Unit, plan_chunks, run_chunked
from steerai.probes.suite_outcome import (
    OUTCOMES,
    Independent,
    Run,
    outcome,
    planner_output_tokens,
    split_ms,
)
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
    "h5_event",
    "h5_final",
    "h6_event",
    "h7_event",
    "gallery_event",
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
    which: tuple[tuple[str, str], ...] = ()
    """The scripted person's answer to a twin ask, per target name: the automation id of the
    field the task means (packet h4-2026-11-11 round 2, item 5). A harness oracle."""
    result_link: str = ""
    """For an instant-search `url_search`: the path prefix of a result's own link (`/pods/`).
    The page lists results as the words are typed and never changes its URL (cocoapods.org,
    V2 step 1 diagnostics 2026-11-13), so a link to `<prefix><words>` on the page is the
    independent read as well as the URL. The start page is read first: one that already links
    to the result makes the run invalid (`result_link_preexisting`). Empty: the URL alone."""


PAGE_SETS = ("h2", "h3", "h4", "h5", "h6", "h7", "gallery")
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
    result_link: str = "",
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
        result_link=result_link,
    )


CHALLENGE_TITLES = frozenset(
    {"Client Challenge", "Just a moment...", "Making sure you're not a bot!"}
)
"""Bot-check interstitials at a results URL (PyPI, Cloudflare, Anubis on the Arch wiki's search
path, H4 aw1 2026-11-11): the run measured nothing."""


_LINKS_JS = "[...document.querySelectorAll('a[href]')].map(a => a.pathname)"


def _read_page(port: int, stage: str, *, links: bool = False) -> str:
    """The page's href and title as JSON (with `links`, its links' paths, at most 500); a bot
    check is an invalid run, never a pass."""
    extra = f", links: {_LINKS_JS}.slice(0, 500)" if links else ""
    expression = f"JSON.stringify({{href: location.href, title: document.title{extra}}})"
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
        proc = _launch_one(
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
            _stop_one(proc, tmp / "profile")
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


def _h5(task_id: str, goal: str, event: dict[str, Any], **kw: Any) -> Task:
    return Task(task_id, goal, "h5", "h5_event", json.dumps(event, sort_keys=True), **kw)


H5: tuple[Task, ...] = (
    _h5(
        "r1", "choose Express in the shipping speed options", {"kind": "speed", "value": "Express"}
    ),
    _h5("r2", "turn on dark mode", {"kind": "dark", "on": True}),
    Task(
        "r3",
        "pick Europe/Berlin as the time zone",
        "h5",
        "h5_final",
        json.dumps(
            {
                "kinds": ["zone"],
                "state": {"dark": False, "newsletter": True, "zone": "Europe/Berlin"},
                "untrusted": False,
            },
            sort_keys=True,
        ),
    ),
    _h5("r4", "export the report as PDF", {"format": "PDF", "kind": "export"}),
    _h5("r5", "unsubscribe from the newsletter", {"kind": "newsletter", "on": False}),
    _url_search(
        "hx1",
        "Hex",
        "https://hex.pm/",
        "ecto",
        path="/packages",
        param="search",
        title="Hex",
    ),
    Task(
        "ub1",
        "search Ubuntu packages for vim",
        "https://packages.ubuntu.com/",
        "url_search",
        "vim",
        frozenset({"vim"}),
        path="/search",
        param="keywords",
        title="Ubuntu \u2013 Package Search Results -- vim",
        which=(("Keyword:", "kw"),),
    ),
    _url_search(
        "mc1",
        "MetaCPAN",
        "https://metacpan.org/",
        "mojolicious",
        path="/search",
        param="q",
        title='Search for "mojolicious" - metacpan.org',
    ),
    _url_search(
        "ng1",
        "NuGet",
        "https://www.nuget.org/",
        "serilog",
        path="/packages",
        param="q",
        title="NuGet Gallery | Packages matching serilog",
    ),
    _url_search(
        "fh1",
        "Flathub",
        "https://flathub.org/",
        "inkscape",
        path="/en/apps/search",
        param="q",
        title="Search for \u201cinkscape\u201d | Flathub",
    ),
)
"""H5 (packet h5-2026-11-11): committed before the fixes for H4's classes B and C, run only
after them. Five widget shapes on one new local page (`h5_page`: radios in a fieldset, an ARIA
switch, grouped select options, a menu button, an uncheck) and five sites no earlier set used
(probe `h5_sites`, Chrome 156, 2026-11-11): hex.pm keeps its home title "Hex" on the results
page (class B's shape, `ComboBox "Find packages"`); packages.ubuntu.com lists two
`Edit "Keyword:"` (ids `kw` for names, on screen, and `keyword` for file contents, below the
fold: class C's shape, the person answers `kw`); MetaCPAN, NuGet and Flathub title their
results with the words."""


def _row(
    task_id: str, goal: str, event: dict[str, Any], approve: frozenset[str] = frozenset()
) -> Task:
    return Task(
        task_id, goal, "gallery", "gallery_event", json.dumps(event, sort_keys=True), approve
    )


GALLERY: tuple[Task, ...] = (
    _row("g01", "click Save draft", {"row": "g01"}),
    _row("g02", "turn on Bold", {"row": "g02", "on": True}),
    _row("g03", "turn on Dark mode", {"row": "g03", "on": True}),
    _row("g04", "tick Email me updates", {"row": "g04", "on": True}),
    _row("g05", "tick Remember this device", {"row": "g05", "on": True}),
    _row("g06", "tick Select all files", {"row": "g06", "on": True}),
    _row("g07", "choose Express as the delivery speed", {"row": "g07", "value": "Express"}),
    _row("g08", "choose Letter as the paper size", {"row": "g08", "value": "Letter"}),
    _row("g09", "open the Billing details tab", {"row": "g09", "value": "Billing details"}),
    _row("g10", "pick Mango as the favourite fruit", {"row": "g10", "value": "Mango"}),
    _row("g11", "pick Poetry on the shelf", {"row": "g11", "value": "Poetry"}),
    _row("g12", "set the country to Kenya", {"row": "g12", "value": "Kenya"}),
    _row("g13", "set the time zone to Europe/Rome", {"row": "g13", "value": "Europe/Rome"}),
    _row("g14", "set the colour to Teal", {"row": "g14", "value": "Teal"}),
    _row("g15", "expand the Shipping policy", {"row": "g15", "open": True}),
    _row("g16", "show the filters", {"row": "g16", "open": True}),
    _row("g17", "copy the link from the Share menu", {"row": "g17", "item": "Copy link"}),
    _row("g18", "turn on Show word count in the Share menu", {"row": "g18", "on": True}),
    _row("g19", "pick Large font in the Share menu", {"row": "g19", "value": "Large font"}),
    _row("g20", "open the Projects folder", {"row": "g20", "open": True}),
    _row("g21", "select Order 1002 in the orders", {"row": "g21", "value": "Order 1002"}),
    _row("g22", "type Ada Lovelace as the full name", {"row": "g22", "value": "Ada Lovelace"}),
    _row(
        "g23",
        "search the articles for automation",
        {"row": "g23", "value": "automation"},
        frozenset({"automation"}),
    ),
    _row("g25", "open the Terms of service", {"row": "g25"}),
)
"""The gallery rows as goals (packet interaction-model-2026-11-12 §5): the regression bar before
H6, 3/3 per row through the planner. Not held out: the gallery is the model's own instrument.
g24 (a textarea, whose `change` fires on blur) and g26/g27 (range: no verb) are not goals."""


def _h6(task_id: str, goal: str, event: dict[str, Any], **kw: Any) -> Task:
    return Task(task_id, goal, "h6", "h6_event", json.dumps(event, sort_keys=True), **kw)


H6: tuple[Task, ...] = (
    _h6("u1", "mute notifications", {"kind": "mute", "on": True}),
    _h6("u2", "turn on Auto-save", {"kind": "autosave", "on": True}),
    _h6("u3", "open the Reports folder and select 2024", {"kind": "year", "value": "2024"}),
    _h6("u4", "select Invoice 2042 in the invoices", {"kind": "invoice", "value": "Invoice 2042"}),
    _h6("u5", "sort the results by Newest first", {"kind": "sort", "value": "Newest first"}),
    _url_search(
        "gd1",
        "Go packages",
        "https://pkg.go.dev/",
        "cobra",
        path="/search",
        param="q",
        title="cobra - Search Results - Go Packages",
    ),
    _url_search(
        "pd1",
        "pub.dev",
        "https://pub.dev/",
        "provider",
        path="/packages",
        param="q",
        title="Search results for provider",
    ),
    _url_search(
        "cp1",
        "CocoaPods",
        "https://cocoapods.org/",
        "alamofire",
        path="/",
        param="q",
        title="CocoaPods.org",
        result_link="/pods/",
    ),
    _url_search(
        "am1",
        "Firefox add-ons",
        "https://addons.mozilla.org/en-US/firefox/",
        "ublock",
        path="/en-US/firefox/search/",
        param="q",
        title='Search results for "ublock" \u2013 Add-ons for Firefox (en-US)',
    ),
    Task(
        "wt1",
        "look up serendipity on Wiktionary",
        "https://en.wiktionary.org/wiki/Wiktionary:Main_Page",
        "wiki_article",
        "/wiki/serendipity",
        frozenset({"serendipity"}),
    ),
)
"""H6 (packet interaction-model-2026-11-12 §6): sealed before the interaction model's verb
admission merges, run once after the gallery is 3/3 per row. Five widget tasks on a new page
(`h6_page`: a toolbar toggle button, a switch on a native checkbox input, a tree folder opened
and a year selected, a div grid row, a menuitemradio in a menu button), each a row of the model's
table in markup no earlier page or the gallery uses; five sites no earlier set used (probe
`h6_sites`, Chrome 156, 2026-11-12): pkg.go.dev and pub.dev title their results with the words,
CocoaPods keeps its home title (class B's shape), Firefox add-ons lists its search Edit over an
unnamed ComboBox, Wiktionary's search lands on the entry."""


def _h7(task_id: str, goal: str, event: dict[str, Any], **kw: Any) -> Task:
    return Task(task_id, goal, "h7", "h7_event", json.dumps(event, sort_keys=True), **kw)


H7_SAVED_TEXT = "steer h7 notes"
H7_SAVED_NAME = "steer-h7.txt"

H7: tuple[Task, ...] = (
    _h7("h7w1", "show the Shipping details", {"kind": "disclosure", "open": True}),
    _h7("h7w2", "choose Teal as the theme colour", {"kind": "colour", "value": "Teal"}),
    _h7("h7w3", "open the Pricing tab", {"kind": "tab", "value": "Pricing"}),
    _h7("h7w4", "turn on High contrast", {"kind": "contrast", "on": True}),
    _h7("h7w5", "set the city to Lisbon", {"kind": "city", "value": "Lisbon"}),
    _h7(
        "h7w6",
        "rename the project to Orion",
        {"kind": "rename", "value": "Orion"},
        approve=frozenset({"Orion"}),
        approve_targets=frozenset({"Save"}),
    ),
    _h7(
        "h7w7",
        "set the text size to Large from the Format menu",
        {"kind": "size", "value": "Large"},
    ),
    _h7("h7w8", "give the recipe 4 stars", {"kind": "rating", "value": 4}),
    _h7(
        "h7c1",
        "send standup moved to 10 in the team chat",
        {"kind": "send", "text": "standup moved to 10"},
        approve=frozenset({"standup moved to 10"}),
        approve_targets=frozenset({"Send"}),
    ),
    _h7(
        "h7c2",
        "delete the draft Q3 plan",
        {"kind": "delete", "value": "Q3 plan"},
        approve_targets=frozenset({"Delete", "Delete Q3 plan"}),
    ),
    Task(
        "h7n1",
        f"type {H7_SAVED_TEXT} in notepad and save it as {H7_SAVED_NAME} in the folder TMPDIR",
        "notepad_save",
        "file_text",
        H7_SAVED_TEXT,
        frozenset({H7_SAVED_TEXT, H7_SAVED_NAME}),
        path=H7_SAVED_NAME,
        approve_targets=frozenset({"Save"}),
    ),
    Task(
        "h7n2",
        "calculate 23 times 4 in calculator",
        "calculator",
        "calc_display",
        json.dumps({"result": "92", "expression": f"23{MULTIPLY}4="}),
    ),
    Task(
        "h7s1",
        "look up ephemeral in the Cambridge Dictionary",
        "https://dictionary.cambridge.org/",
        "wiki_article",
        "/dictionary/english/ephemeral",
        frozenset({"ephemeral"}),
    ),
    _url_search(
        "h7s2", "Packagist", "https://packagist.org/", "monolog", path="/search/", param="query"
    ),
    _url_search(
        "h7s3",
        "the Django documentation",
        "https://docs.djangoproject.com/en/stable/",
        "middleware",
        path="/en/5.2/search/",
    ),
    _url_search("h7s4", "Open Library", "https://openlibrary.org/", "dune", path="/search"),
    Task(
        "h7s5",
        "go to the next page of Hacker News",
        "https://news.ycombinator.com/news",
        "url_search",
        "2",
        path="/news",
        param="p",
    ),
    Task(
        "h7s6",
        "open the Windows downloads page from the Downloads menu on python.org",
        "https://www.python.org/",
        "wiki_article",
        "/downloads/windows/",
    ),
    Task(
        "h7s7",
        "sort the Codeberg repositories by most stars",
        "https://codeberg.org/explore/repos",
        "url_search",
        "moststars",
        path="/explore/repos",
        param="sort",
    ),
    Task(
        "h7s8",
        "look up Lisbon on Wikivoyage",
        "https://en.wikivoyage.org/wiki/Main_Page",
        "wiki_article",
        "/wiki/Lisbon",
        frozenset({"lisbon"}),
    ),
)
"""H7 (V2 §12 step 8, [V2.4]): sealed before any V2 step 3 code, written without looking at v2
runs; 20 tasks, run twice each, counted per task (both runs pass) and per run. Eight widget
rows and two commit fixtures on a new local page (`h7_page`); two native tasks (a Notepad save
read from the file, #202, and Calculator); eight sites no earlier set used, chosen by category
(dictionary, package registry, docs site, library catalogue, news aggregator, project site,
code host, travel guide), never by how their search behaves: three are not a search (h7s5 the
next page, h7s6 a section from a menu, h7s7 a sort). A site's probe checks only that it is
reachable, needs no login, shows no bot wall and its positive control passes; behaviour is
recorded, never selected on. A task whose positive control fails on run day is void, never
replaced. Bars: false done 0, unsafe 0, success >= 80% with its interval."""


H7_DRIVES: dict[str, tuple[tuple[str, str], ...]] = {
    "h7w1": (("click", "#ship-toggle"),),
    "h7w2": (("click", "#col-teal"),),
    "h7w3": (("click", "#tab-pricing"),),
    "h7w4": (("click", "#contrast-host"),),
    "h7w5": (("focus", "#city"), ("click", "#city-lisbon")),
    "h7w6": (("click", "#rename-open"), ("type", "#rename-field|Orion"), ("click", "#rename-save")),
    "h7w7": (("click", "#format"), ("click", "#text-size"), ("click", "#size-large")),
    "h7w8": (("click", '#stars span[data-v="4"]'),),
    "h7c1": (("type", "#chat-box|standup moved to 10"), ("click", "#chat-send")),
    "h7c2": (("click", "#del-q3-plan"), ("click", "#confirm-yes")),
}
"""What a person would do for each H7 page task, as trusted CDP input (positive control only)."""


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


H5_DRIVES: dict[str, tuple[tuple[str, str], ...]] = {
    "r1": (("click", "#s-express"),),
    "r2": (("click", "#dark"),),
    "r3": (("focus", "#zone"), ("char", "E")),
    "r4": (("click", "#export"), ("click", "#x-pdf")),
    "r5": (("click", "#newsletter"),),
}
"""What a person would do for each H5 page task, as trusted CDP input (positive control only):
r3 types the first letter of the wanted zone group's first option (Europe/Berlin is first in
its group); r4 opens the menu, then picks PDF."""


H6_DRIVES: dict[str, tuple[tuple[str, str], ...]] = {
    "u1": (("click", "#mute"),),
    "u2": (("click", "#autosave"),),
    "u3": (("click", "#reports > span"), ("click", "#y2024")),
    "u4": (("click", "#inv-2042"),),
    "u5": (("click", "#sort"), ("click", "#sort-newest")),
}
"""What a person would do for each H6 page task, as trusted CDP input (positive control only)."""


def h7_controls() -> list[str]:
    """Each H7 page task done by scripted trusted input and judged by `passed`; the site tasks
    by opening their results URL or the target page."""
    return _page_controls(H7, H7_DRIVES) + controls(
        [task for task in H7 if task.check in ("url_search", "wiki_article")]
    )


def h6_controls() -> list[str]:
    """Each H6 page task done by scripted trusted input and judged by `passed`; the site tasks
    by opening their results URL or the entry."""
    return _page_controls(H6, H6_DRIVES) + controls(
        [task for task in H6 if task.check in ("url_search", "wiki_article")]
    )


def h5_controls() -> list[str]:
    """Each H5 page task done by scripted trusted input and judged by `passed`; the site tasks
    by opening their results URL."""
    return _page_controls(H5, H5_DRIVES) + controls(
        [task for task in H5 if task.check in ("url_search", "wiki_article")]
    )


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
        with PageServer(_pages_for(task)) as server:
            port = _free_port()
            url = f"http://127.0.0.1:{server.port}/page.html"
            proc = _launch_one(
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
                _stop_one(proc, tmp / "profile")
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


def _pages_for(task: Task) -> dict[str, str]:
    """Every page a page task's server holds: the held-out page, and the gallery's own extras."""
    if task.start == "gallery":
        from steerai.probes.gallery_page import pages  # noqa: PLC0415 - the gallery only

        return pages()
    return {"/page.html": _page_for(task)}


def _page_for(task: Task) -> str:  # noqa: PLR0911 - one per held-out page
    """The held-out page a page task runs on (H2, H3 or H4)."""
    if task.start == "gallery":
        from steerai.probes.gallery_page import page_html as gallery_html  # noqa: PLC0415

        return gallery_html()
    if task.start == "h7":
        from steerai.probes.h7_page import page_html as h7_html  # noqa: PLC0415 - H7 only

        return h7_html()
    if task.start == "h6":
        from steerai.probes.h6_page import page_html as h6_html  # noqa: PLC0415 - H6 only

        return h6_html()
    if task.start == "h5":
        from steerai.probes.h5_page import page_html as h5_html  # noqa: PLC0415 - H5 only

        return h5_html()
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
    if task.check in ("h3_final", "h4_final", "h5_final"):
        return _read(port, "JSON.stringify(window.finalState())", "final")
    key = _page_log_key(task)
    return h2_event(_read(port, f'localStorage.getItem("{key}") || "[]"', "read"))


def _page_log_key(task: Task) -> str:  # noqa: PLR0911 - one per held-out page
    if task.start == "gallery":
        from steerai.probes.gallery_page import LOG_KEY as GALLERY_KEY  # noqa: PLC0415

        return GALLERY_KEY
    if task.start == "h7":
        from steerai.probes.h7_page import LOG_KEY as H7_KEY  # noqa: PLC0415 - H7 only

        return H7_KEY
    if task.start == "h6":
        from steerai.probes.h6_page import LOG_KEY as H6_KEY  # noqa: PLC0415 - H6 only

        return H6_KEY
    if task.start == "h5":
        from steerai.probes.h5_page import LOG_KEY as H5_KEY  # noqa: PLC0415 - H5 only

        return H5_KEY
    if task.start == "h4":
        from steerai.probes.h4_page import LOG_KEY as H4_KEY  # noqa: PLC0415 - H4 only

        return H4_KEY
    if task.start == "h3":
        from steerai.probes.h3_page import LOG_KEY as H3_KEY  # noqa: PLC0415 - H3 only

        return H3_KEY
    from steerai.probes.h2_page import LOG_KEY as H2_KEY  # noqa: PLC0415 - H2 only

    return H2_KEY


def task_set(name: str) -> tuple[Task, ...]:
    """The set by its `--set` name; `dev` and anything unknown are the development set."""
    sets = {
        "gallery": GALLERY,
        "h7": H7,
        "h6": H6,
        "h5": H5,
        "h4": H4,
        "h3": H3,
        "h2": H2,
        "held_out": HELD_OUT,
    }
    return sets.get(name, TASKS)


def _folded(text: str) -> str:
    return " ".join(text.split()).casefold()


MIN_TARGET_CHARS = 3
"""A target shorter than this never matches the goal: "Go" is inside "Google" (#204 review)."""


def _named_in_goal(target: str, goal: str) -> bool:
    """The goal rule (#200, #204 review): the target's folded tokens appear as a contiguous
    token sequence in the folded goal, never as a substring of a token, and the target holds
    at least MIN_TARGET_CHARS. "Annual" and "I agree to the terms" pass; "Go" and "Post" as
    a prefix do not."""
    wanted = _folded(target).split()
    if not wanted or len("".join(wanted)) < MIN_TARGET_CHARS:
        return False
    words = _folded(goal).split()
    size = len(wanted)
    return any(words[i : i + size] == wanted for i in range(len(words) - size + 1))


@dataclass
class Person:
    """`go` to every plan; `yes` only to an ask whose text is one of the task's exact phrases,
    or to a text-less ask whose target is a listed name or is written verbatim in the goal
    (#200: the goal "tick I agree to the terms" names the box the gate asks about)."""

    approve: frozenset[str]
    approve_targets: frozenset[str] = frozenset()
    goal: str = ""
    which: Mapping[str, str] = field(default_factory=dict)
    """A twin ask's answer per target name: the automation id of the field the task means
    (packet h4-2026-11-11 round 2, item 5). A harness oracle; the product never chooses by it."""
    log: list[dict[str, Any]] = field(default_factory=list)

    def ask(self, ask: Any) -> str:  # Any: steerai.orchestrator.gate.Ask (no import cycle)
        decision = str(ask.decision_id)
        if getattr(ask, "kind", "yes") == "which":
            wanted_id = self.which.get(str(getattr(ask, "target_name", "")))
            chosen = [
                c.index
                for c in getattr(ask, "candidates", ())
                if wanted_id and getattr(c, "automation_id", "") == wanted_id
            ]
            self.log.append(
                {"verb": str(ask.verb), "risk": str(ask.risk), "approved": bool(chosen)}
            )
            return f"{chosen[0]}:{decision}" if len(chosen) == 1 else f"no:{decision}"
        text = ask.params.get("text") if hasattr(ask, "params") else None
        wanted = {_folded(a) for a in self.approve}
        target = str(getattr(ask, "target_name", ""))
        in_goal = _named_in_goal(target, self.goal)
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


def shows_result(task: Task, page: Mapping[str, Any]) -> bool:
    """The page links to the searched result's own page (`result_link` + the words, casefolded
    equality: a neighbour such as `/pods/AlamofireImage` is not it)."""
    if not task.result_link:
        return False
    wanted = (task.result_link + task.expected).casefold()
    return any(str(link).casefold() == wanted for link in page.get("links") or [])


def passed(task: Task, status: str | None, observed: str) -> bool:  # noqa: PLR0911 - one per check
    """Steer said `done` and the independent read agrees exactly (#139 review item 3)."""
    if status != "done":
        return False
    if task.check == "calc_display":
        return bool(json.loads(observed or "{}") == json.loads(task.expected))
    if task.check in ("h3_final", "h4_final", "h5_final"):
        return bool(json.loads(observed or "{}") == json.loads(task.expected))
    if task.check in (
        "notepad_text",
        "app_text",
        "h2_event",
        "h3_event",
        "h4_event",
        "h5_event",
        "h6_event",
        "h7_event",
        "gallery_event",
    ):
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
        by_url = path == task.path and words.casefold() == task.expected.casefold() and same_title
        return by_url or shows_result(task, page)
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
    counts = {name: sum(r.get("outcome") == name for r in measured) for name in OUTCOMES}
    shown = ", ".join(f"{name} {n}" for name, n in counts.items() if n) or "none"
    lines.append(f"outcomes: {shown}")
    lines.append(f"false_done: {counts['false_done']} (the bar is 0)")
    lines.append(f"all: {total}/{len(measured)}")  # last: readers take the total from it
    return lines, ok


class _Planner:
    """The real planner, counted; a refused plan's boundary path and reason are kept from the
    error's cause (Steer's trace keeps the path only: the reason may quote the model's text),
    and an empty plan's screen (title and lines) is kept for `--keep` (#201: the model was
    asked twice and answered empty twice, so what it saw is the cause to read)."""

    def __init__(self) -> None:
        self.calls = 0
        self.refusals: list[tuple[int, str, str]] = []
        self.empty: list[tuple[str, list[str]]] = []

    def __call__(self, request: PlanRequest) -> PlanOutcome:
        self.calls += 1
        try:
            return run_mod._planner(request)
        except PlannerError as exc:
            cause = exc.__cause__
            if isinstance(cause, PlanError):
                self.refusals.append((exc.attempt, cause.path, cause.reason))
            if exc.detail == "no_steps":
                self.empty.append((request.window_title, list(request.screen_lines)))
            raise


def empty_lines(planner: _Planner, before: int) -> list[int]:
    """The line counts of the screens empty plans were given since `before`: the row's share
    (the text goes to `--keep` only)."""
    return [len(lines) for _title, lines in planner.empty[before:]]


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


KEPT_FILES: Final = ("trace.jsonl", "trace.debug.jsonl.dpapi")
"""What `--keep` copies besides the plans: the clear trace and the debug file as it is, still
DPAPI-protected (never decrypted into the keep folder, OB-07)."""
_LEVEL: dict[str, TraceLevel] = {"trace_level": "redacted"}
"""This invocation's trace level: `debug` only with `--keep` (set by `main`), since the debug
file lives in the run's temp folder and is removed with it otherwise."""


def _options(tmp: Path, hwnd: int, task: Task, number: int) -> run_mod.Options:
    return run_mod.Options(
        trace_level=_LEVEL["trace_level"],
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
    person = Person(task.approve, task.approve_targets, goal=task.goal, which=dict(task.which))
    deps = run_mod._real_deps().replace(
        make_confirmer=lambda *_: person,
        open_windows=lambda: [],
        planner=planner,
        plan_cache=None,  # never the person's cache, never a replayed plan
    )
    stdout = io.StringIO()
    started = time.monotonic()
    calls_before = planner.calls
    refusals_before = len(planner.refusals)
    empty_before = len(planner.empty)
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
    seconds = time.monotonic() - started
    tokens, incomplete = planner_output_tokens(records)
    try:
        split: dict[str, Any] = dict(split_ms(records, seconds=seconds))
    except ValueError:
        split = {"error": "split_refused"}  # a stage unmapped, or double counting: never a crash
    return {
        "expects": _plan_kinds(tmp),
        "steps": _steps(records),
        "status": result.get("status"),
        "detail": result.get("detail"),
        "seconds": round(seconds, 1),
        "split_ms": split,
        "planner_output_tokens": tokens,
        **({"tokens_incomplete": True} if incomplete else {}),
        "asks": "".join("y" if a["approved"] else "n" for a in person.log),
        "opens_recorded": opened,
        "planner_calls": planner.calls - calls_before,
        "refusals": [list(r) for r in planner.refusals[refusals_before:]],
        "empty_screens": empty_lines(planner, empty_before),
    }


def _run_app(  # noqa: PLR0913 - one app's start and read, passed once
    task: Task, number: int, tmp: Path, planner: _Planner, *, exe: Path, edit_class: str
) -> dict[str, Any]:
    proc = subprocess.Popen([str(exe)])  # noqa: S603 - a fixed system path
    try:
        window = wait_window_for_pid(proc.pid, image=exe.name, process=proc)
        _in_front(window.hwnd)
        row = _steer(task, number, window.hwnd, tmp, planner)
        _read_into(row, lambda: notepad_text(window.hwnd, edit_class))
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
    proc = _launch_one(CHROME_DEFAULT, tmp / "profile", port, url, window_size=WINDOW)
    try:
        window = wait_window_for_pid(proc.pid, image="chrome.exe", process=proc)
        page_ws_url(port)  # the page target exists before Steer looks
        # the document is loaded before Steer's first observe: two YouTube dev runs planned on
        # a listing ~700 input tokens short of the rendered page (2026-11-11), and the plan was
        # an open instead of a search; the settled page then lays out (unmeasured; the warmup
        # follows and demands three agreeing node counts)
        _settled(port, SETTLE_LIMIT_S)
        time.sleep(3.0)
        _not_shown_before(task, port)
        _in_front(window.hwnd)
        row = _steer(task, number, window.hwnd, tmp, planner)
        if task.check == "inbox_send":
            _read_into(
                row,
                lambda: inbox_send(
                    _read(port, 'localStorage.getItem("steer-present") || "[]"', "read")
                ),
            )
        elif task.check in (
            "h2_event",
            "h3_event",
            "h3_final",
            "h4_event",
            "h4_final",
            "h5_event",
            "h5_final",
            "h6_event",
            "h7_event",
            "gallery_event",
        ):
            _read_into(row, lambda: _page_observed(task, port))
        elif task.check == "inbox_chat":
            _read_into(row, lambda: _read(port, "document.body.dataset.chat", "chat"))
        else:
            _read_into(row, lambda: _read_page(port, "page", links=bool(task.result_link)))
        return row
    finally:
        _stop_one(proc, tmp / "profile")


TASK_CAP_S = 600.0
"""UNMEASURED guard: the per-task wall-time cap in chunked mode ([V2.14] 4); a chunk's child is
killed after `TASK_CAP_S` x its task count. Override with `--task-cap-s`."""


def _launch_one(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:  # Any: launch_chrome's
    """`launch_chrome` holding the process's one browser slot ([V2.14] 4); `_stop_one` frees it."""
    claim_browser()
    try:
        return launch_chrome(*args, **kwargs)
    except BaseException:
        release_browser()
        raise


def _stop_one(proc: subprocess.Popen[bytes], profile: Path) -> None:
    """Stop this run's own Chrome tree and profile, then free the browser slot."""
    from steerai.probes.chrome_a11y import stop_chrome  # noqa: PLC0415 - with launch

    try:
        stop_chrome(proc, profile)
    finally:
        release_browser()


_STOP_OUTCOMES = frozenset({"false_stop", "true_stop", "stop_unread"})


def _invalid(task: Task, number: int, exc: InvalidRun) -> dict[str, Any]:
    """A run that measured nothing: reported, never a pass, its outcome `invalid`."""
    return {
        "task": task.task_id,
        "run": number,
        "passed": False,
        "invalid": str(exc),
        "outcome": "invalid",
    }


def _not_shown_before(task: Task, port: int) -> None:
    """The `result_link` oracle's before (#261 review item 1): the start page must not already
    link to the result, or the read after the run proves nothing; such a run is invalid."""
    if task.result_link and shows_result(
        task, json.loads(_read_page(port, "before", links=True) or "{}")
    ):
        raise InvalidRun("result_link_preexisting")


def _read_into(row: dict[str, Any], read: Callable[[], str]) -> None:
    """The independent read into the row (packet §D, re-review item 3): its value; a run found
    invalid during the read (`InvalidRun`: a second tab, a bot check) is a post-run invalid
    that keeps Steer's verdict; a read that failed otherwise is `unread`, by exception type
    only (a message can quote the page). Anything else is a bug and propagates."""
    try:
        row["observed"] = read()
    except InvalidRun as exc:
        row["post_invalid"] = str(exc)
    except (OSError, ValueError, KeyError) as exc:
        row["unread"] = type(exc).__name__


def one_run(
    task: Task,
    number: int,
    planner: _Planner,
    keep: Path | None = None,
    *,
    tmp_root: Path | None = None,
) -> dict[str, Any]:
    """One task run; its temp folder sits under `tmp_root` (a chunk's own folder) when given."""
    try:
        wait_until_idle()
    except InvalidRun as exc:
        # the person stayed active: this run measured nothing, the suite goes on
        return _invalid(task, number, exc)
    tmp = Path(tempfile.mkdtemp(prefix="steer-suite-", dir=tmp_root))
    empty_before = len(planner.empty)
    try:
        row = _started(task, number, tmp, planner)
        if keep is not None:
            for index, (title, lines) in enumerate(planner.empty[empty_before:], start=1):
                text = "".join(line + "\n" for line in [title, *lines])
                # LF on every OS; a lone surrogate from UIA is escaped, never a suite crash
                (keep / f"{task.task_id}-{number}-empty-{index}.txt").write_bytes(
                    text.encode("utf-8", errors="backslashreplace")
                )
            # diagnosis only: the plans and trace of a fixture goal, into a local folder
            for kept in [*tmp.glob("*.plan*.json"), *(tmp / name for name in KEPT_FILES)]:
                if kept.is_file():
                    shutil.copy(kept, keep / f"{task.task_id}-{number}-{kept.name}")
    except InvalidRun as exc:
        # the run measured nothing (the window never came to front): reported, never a pass
        return _invalid(task, number, exc)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    observed = row.pop("observed", None)
    independent: Independent
    if observed is None or "unread" in row or "post_invalid" in row:
        independent = "unread"
    else:
        independent = "ok" if passed(task, "done", str(observed)) else "not"
    ok = (
        independent != "unread"
        and passed(task, row["status"], str(observed))
        and row["opens_recorded"] == 0
    )
    row["independent"] = independent
    row["outcome"] = outcome(
        Run(
            status=row["status"],
            independent=independent,
            detail=row.get("detail"),
            opens=int(row.get("opens_recorded") or 0),
            post_invalid="post_invalid" in row,
        )
    )
    if row["outcome"] in _STOP_OUTCOMES and "n" in str(row.get("asks") or ""):
        row["declined"] = True  # the scripted person said no: not Steer's stop alone (OB-37)
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
        _read_into(row, lambda: calc_display(_calc_names(window.hwnd)))
        return row
    finally:
        _close(window.hwnd)


SAVE_ROOT = Path(Path(__file__).anchor) / "steer-probe"
"""Save into a folder on the repository's drive with no user name in its path: the goal goes to
the planner and any kept trace (#154 review item 4)."""
STRAY_ROOT = Path.home() / "Documents"
"""Where a Save that ignored the folder lands by default: a file there fails the run."""


def _run_notepad_save(task: Task, number: int, tmp: Path, planner: _Planner) -> dict[str, Any]:
    """Notepad, then its Save dialog: the saved file in the run's own folder is the read. The
    file name is the task's `path` (H7 h7n1), or save1's."""
    name = task.path or SAVED_NAME
    stray = STRAY_ROOT / name
    if stray.exists():
        raise InvalidRun("stray_file_exists")  # never the person's file: measure nothing
    folder = SAVE_ROOT / tmp.name
    folder.mkdir(parents=True)
    goal = task.goal.replace("TMPDIR", str(folder))
    proc = subprocess.Popen([str(NOTEPAD)])  # noqa: S603 - a fixed system path
    try:
        window = wait_window_for_pid(proc.pid, image="notepad.exe", process=proc)
        _in_front(window.hwnd)
        row = _steer(replace(task, goal=goal), number, window.hwnd, tmp, planner)
        saved = folder / name
        if stray.exists():
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
        with PageServer(_pages_for(task)) as server:
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
    parser.add_argument(
        "--set",
        choices=("dev", "held_out", "h2", "h3", "h4", "h5", "h6", "h7", "gallery"),
        default="dev",
    )
    parser.add_argument(
        "--controls", action="store_true", help="open each search task's results URL instead"
    )
    parser.add_argument("--tasks", default="", help="comma-separated task ids (default: all)")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--keep", type=Path, default=None, help="copy plans and traces here")
    parser.add_argument(
        "--min-free-mb",
        type=int,
        default=FLOOR_MB,
        help=f"refuse to start a task below this free memory (default {FLOOR_MB}, unmeasured)",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=None,
        help="run this many tasks per child process, one child at a time (1: one per process)",
    )
    parser.add_argument(
        "--task-cap-s",
        type=float,
        default=None,
        help=f"per-task wall-time cap, chunked (default {TASK_CAP_S:g}; implies --chunk-size 1)",
    )
    parser.add_argument(
        "--in-process",
        action="store_true",
        help="run every task in this process, no wall-time cap (debugging only; [V2.16])",
    )
    parser.add_argument("--run-number", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--tmp-root", type=Path, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    wanted = set(filter(None, args.tasks.split(",")))
    tasks = task_set(args.set)
    if args.controls:
        lines = _control_lines(args.set, tasks)
        sys.stdout.write("\n".join(lines) + "\n")
        return 0 if all(" control passes" in line for line in lines) else 1
    _LEVEL["trace_level"] = "redacted" if args.keep is None else "debug"
    if args.keep is not None:
        args.keep.mkdir(parents=True, exist_ok=True)  # never a lost row after the run (#249)
    numbers = [args.run_number] if args.run_number is not None else range(1, args.runs + 1)
    units = [(n, t) for n in numbers for t in tasks if not wanted or t.task_id in wanted]
    if args.in_process and (args.chunk_size is not None or args.task_cap_s is not None):
        parser.error("--in-process takes no --chunk-size or --task-cap-s")
    if not args.in_process and args.run_number is None:
        args.chunk_size = 1 if args.chunk_size is None else args.chunk_size  # [V2.16] default
        return _chunked(args, units, tasks)
    planner = _Planner()
    rows: list[dict[str, Any]] = []
    with args.out.open("w", encoding="utf-8") as out:
        out.write(json.dumps({**_header(), "mode": IN_PROCESS}) + "\n")
        for index, (number, task) in enumerate(units):
            try:
                preflight(floor_mb=args.min_free_mb, read=free_physical_mb)
            except LowMemory as refused:
                remaining = [f"{t.task_id}:{n}" for n, t in units[index:]]
                refusal = {**refusal_row(refused, remaining), "mode": IN_PROCESS}
                out.write(json.dumps(refusal) + "\n")
                sys.stderr.write(f"{refused}\n")
                return EXIT_LOW_MEMORY
            row = {**one_run(task, number, planner, args.keep, tmp_root=args.tmp_root)}
            row["mode"] = IN_PROCESS
            rows.append(row)
            out.write(json.dumps(row) + "\n")
            out.flush()
    lines, ok = verdict(rows, tasks)
    lines.append(seconds_line(rows, tasks))
    sys.stdout.write("\n".join(lines) + f"\nbar {'met' if ok else 'missed'}\n")
    return 0 if ok else 1


IN_PROCESS = "in_process"
CHUNKED = "chunked"
"""[V2.16] the run mode every row records, so the two modes are never mixed unseen."""


def split_infrastructure(
    rows: Sequence[dict[str, Any]], *, set_name: str
) -> tuple[list[dict[str, Any]], str]:  # Any: the suite's JSON rows
    """[V2.16] the rows the verdict scores, and one line for the infrastructure bucket. Aborted
    and refused rows never count as a Steer outcome; on the sealed H7 set a task that hit the
    wall-time cap after it started counts as not done (never re-run), while a task refused
    before it started may run later."""
    scored: list[dict[str, Any]] = []
    aborted: dict[str, int] = {}
    refused = 0
    for row in rows:
        if "refused" in row:
            refused += 1
        elif "aborted" in row:
            reason = str(row["aborted"])
            aborted[reason] = aborted.get(reason, 0) + 1
            if set_name == "h7" and reason == "task_cap":
                scored.append({**row, "passed": False, "outcome": "interrupted"})
        elif "outcome" in row:
            scored.append(row)
    detail = ", ".join(f"{k} {n}" for k, n in sorted(aborted.items()))
    shown = f"aborted {sum(aborted.values())}" + (f" ({detail})" if detail else "")
    return scored, f"infrastructure: {shown}, refused {refused}"


def _control_lines(name: str, tasks: Sequence[Task]) -> list[str]:
    by_set: dict[str, Callable[[], list[str]]] = {
        "h2": h2_controls,
        "h3": h3_controls,
        "h7": h7_controls,
        "h6": h6_controls,
        "h5": h5_controls,
        "h4": h4_controls,
    }
    control = by_set.get(name)
    return control() if control is not None else controls(tasks)


def _chunked(
    args: argparse.Namespace, units: Sequence[tuple[int, Task]], tasks: Sequence[Task]
) -> int:
    """[V2.14] 4: each chunk in its own child process, one at a time, with the pre-flight floor
    and the per-task cap; the rows land in `--out` as they come."""
    base = ["--set", args.set, *(["--keep", str(args.keep)] if args.keep is not None else [])]
    chunks = plan_chunks([Unit(t.task_id, n) for n, t in units], args.chunk_size or 1)
    rows: list[dict[str, Any]] = []
    tmp_parent = Path(tempfile.mkdtemp(prefix="steer-chunks-"))
    try:
        with args.out.open("w", encoding="utf-8") as out:
            out.write(json.dumps({**_header(), "mode": CHUNKED}) + "\n")

            def write_row(row: dict[str, Any]) -> None:
                marked = {**row, "mode": CHUNKED}
                rows.append(marked)
                out.write(json.dumps(marked) + "\n")
                out.flush()

            code = run_chunked(
                chunks,
                base_args=base,
                cap_s=args.task_cap_s if args.task_cap_s is not None else TASK_CAP_S,
                floor_mb=args.min_free_mb,
                tmp_parent=tmp_parent,
                write_row=write_row,
            )
    finally:
        shutil.rmtree(tmp_parent, ignore_errors=True)
    if code == EXIT_LOW_MEMORY:
        return EXIT_LOW_MEMORY
    scored, infrastructure = split_infrastructure(rows, set_name=args.set)
    lines, ok = verdict(scored, tasks)
    lines.append(seconds_line(scored, tasks))
    lines.append(infrastructure)
    sys.stdout.write("\n".join(lines) + f"\nbar {'met' if ok else 'missed'}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
