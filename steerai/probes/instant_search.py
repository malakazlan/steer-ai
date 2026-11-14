"""Step 3d: the local instant-search fixture ([V2.12]; step 3 packet §5 cases (a) to (d)).

One generic catalog page in five variants, served from memory on 127.0.0.1 (no network):

- `a`: results and `?q=` while typing;
- `b`: results while typing, the URL unchanged;
- `c-button`: typing does nothing, Enter is a no-op, a Search button shows the results and `?q=`;
- `c-nobutton`: typing does nothing, Enter is a no-op, there is no button: no end state is
  reachable;
- `d`: slow instant search: results and `?q=` appear `delay_ms` after the last keystroke, so
  they land after the type's quiet window; Enter is a no-op.

The page logs its own effects in origin storage as code-owned booleans, never text; a probe reads
the log back over DevTools. `passes` is the pass check per variant. It shares no code with the
v2 loop or the verifier, and nothing here comes from the sealed H7 pages.
"""

from __future__ import annotations

import json
import shutil
import socket
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, get_args

if TYPE_CHECKING:
    from steerai.probes.chrome_a11y import CdpSession

Variant = Literal["a", "b", "c-button", "c-nobutton", "d"]
VARIANTS: tuple[Variant, ...] = get_args(Variant)
EventKind = Literal["results", "enter", "button"]

LOG_KEY = "steer-instant-search"
QUERY = "red shoes"
"""The words the positive controls type, and the query a pass needs the results to be for."""
CATALOG = ("Red shoes", "Red shoes, size 9", "Blue shoes", "Green hat", "Red scarf")
SLOW_DELAY_MS = 1500
"""Variant (d)'s result delay: after the 500 ms quiet window of a `type`, before a 2.5 s
deadline. A page parameter: `render` and `pages` take another."""
MAX_DELAY_MS = 10_000
SETTLE_S = 1.0
"""How long a control waits after its last input before it reads the log (plus the delay)."""

_INSTANT: Mapping[Variant, bool] = {
    "a": True,
    "b": True,
    "c-button": False,
    "c-nobutton": False,
    "d": True,
}
_URL_QUERY: Mapping[Variant, bool] = {
    "a": True,
    "b": False,
    "c-button": True,
    "c-nobutton": False,
    "d": True,
}

_PAGE = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Catalog</title></head>
<body>
<main>
<h1>Catalog</h1>
<form id="find" role="search" action="#">
<label for="q">Search the catalog</label>
<input id="q" name="q" type="search" autocomplete="off">
__BUTTON__
</form>
<p id="count" role="status"></p>
<ul id="results" aria-label="Results"></ul>
</main>
<script>
(function () {
  var KEY = __KEY__, EXPECTED = __EXPECTED__, CATALOG = __CATALOG__;
  var INSTANT = __INSTANT__, URL_QUERY = __URL_QUERY__, DELAY = __DELAY__;
  var field = document.getElementById("q"), list = document.getElementById("results");
  var count = document.getElementById("count"), go = document.getElementById("go");
  var timer = null, lastInput = 0;
  function put(entry) {
    var log = JSON.parse(localStorage.getItem(KEY) || "[]");
    log.push(entry);
    localStorage.setItem(KEY, JSON.stringify(log));
  }
  function words(text) {
    return text.toLowerCase().split(/[^a-z0-9]+/).filter(function (w) { return w; });
  }
  function show(trusted) {
    var query = field.value, wanted = words(query);
    var found = CATALOG.filter(function (item) {
      var have = words(item);
      return wanted.length > 0 && wanted.every(function (w) { return have.indexOf(w) >= 0; });
    });
    list.textContent = "";
    found.forEach(function (item) {
      var li = document.createElement("li");
      li.textContent = item;
      list.appendChild(li);
    });
    count.textContent = found.length + " results";
    if (URL_QUERY) {
      history.replaceState(null, "", "?q=" + encodeURIComponent(query));
    }
    put({ev: "results", match: query.trim().toLowerCase() === EXPECTED,
         url_query: location.search.indexOf("q=") >= 0, trusted: trusted,
         late: Date.now() - lastInput >= DELAY});
  }
  document.getElementById("find").addEventListener("submit", function (e) {
    e.preventDefault();
  });
  field.addEventListener("keydown", function (e) {
    if (e.key === "Enter") {
      put({ev: "enter", match: false, url_query: false, trusted: e.isTrusted, late: false});
    }
  });
  field.addEventListener("input", function (e) {
    lastInput = Date.now();
    if (!INSTANT) { return; }
    var trusted = e.isTrusted;
    clearTimeout(timer);
    timer = setTimeout(function () { show(trusted); }, DELAY);
  });
  if (go) {
    go.addEventListener("click", function (e) {
      put({ev: "button", match: false, url_query: false, trusted: e.isTrusted, late: false});
      show(e.isTrusted);
    });
  }
  document.body.dataset.ready = "1";
})();
</script>
</body>
</html>
"""


@dataclass(frozen=True, slots=True)
class PageSpec:
    variant: Variant
    delay_ms: int = 0


def spec(variant: Variant, *, slow_delay_ms: int = SLOW_DELAY_MS) -> PageSpec:
    """The variant's page: only (d) has a delay."""
    return PageSpec(variant, slow_delay_ms if variant == "d" else 0)


def path_of(variant: Variant) -> str:
    return f"/{variant}"


def render(page: PageSpec, *, expected: str = QUERY) -> str:
    """The variant's HTML. Every value goes into the script as JSON; nothing is fetched."""
    if page.variant not in VARIANTS:
        raise ValueError(f"unknown variant: {page.variant!r}")
    if not 0 <= page.delay_ms <= MAX_DELAY_MS:
        raise ValueError("delay_ms is out of range")
    button = '<button type="button" id="go">Search</button>' if page.variant == "c-button" else ""
    values = {
        "__BUTTON__": button,
        "__KEY__": json.dumps(LOG_KEY),
        "__EXPECTED__": json.dumps(expected.strip().lower()),
        "__CATALOG__": json.dumps(list(CATALOG)),
        "__INSTANT__": json.dumps(_INSTANT[page.variant]),
        "__URL_QUERY__": json.dumps(_URL_QUERY[page.variant]),
        "__DELAY__": json.dumps(page.delay_ms),
    }
    html = _PAGE
    for token, value in values.items():
        html = html.replace(token, value)
    return html


def pages(*, slow_delay_ms: int = SLOW_DELAY_MS) -> dict[str, str]:
    """Every variant's page by its served path."""
    return {path_of(v): render(spec(v, slow_delay_ms=slow_delay_ms)) for v in VARIANTS}


@dataclass(frozen=True, slots=True)
class Entry:
    """One effect the page logged: closed booleans only."""

    ev: EventKind
    match: bool
    """The results were for the expected query."""
    url_query: bool
    """The page's URL carried `q=` when the results were shown."""
    trusted: bool
    """The triggering event came from real (or DevTools Input) input, not from a script."""
    late: bool
    """The results came at least the page's delay after the last keystroke."""


def parse_log(raw: str) -> tuple[Entry, ...]:
    """The page's log as read over DevTools; any other shape is an error, never a pass."""
    data = json.loads(raw or "[]")
    if not isinstance(data, list):
        raise ValueError("the effect log is not a list")
    entries: list[Entry] = []
    for item in data:
        if not isinstance(item, dict) or item.get("ev") not in get_args(EventKind):
            raise ValueError("an effect entry has an unknown shape")
        flags = [item.get(k) for k in ("match", "url_query", "trusted", "late")]
        bools = [f for f in flags if isinstance(f, bool)]
        if len(bools) != len(flags):
            raise ValueError("an effect entry flag is not a boolean")
        match, url_query, trusted, late = bools
        entries.append(Entry(item["ev"], match, url_query, trusted, late))
    return tuple(entries)


def passes(variant: Variant, entries: Sequence[Entry]) -> bool:
    """The variant's intended end state, from the page's own log:

    - `a`: results for the query with `?q=`, and no Enter;
    - `b`: results for the query, and the URL never carried `q=`;
    - `c-button`: the button was used and results for the query came with `?q=`;
    - `c-nobutton`: an Enter was pressed and no results ever came;
    - `d`: results for the query with `?q=`, at least the delay after the last keystroke.

    Every logged event must be trusted.
    """
    if not entries or not all(e.trusted for e in entries):
        return False
    results = [e for e in entries if e.ev == "results"]
    enters = [e for e in entries if e.ev == "enter"]
    hit = [e for e in results if e.match]
    if variant == "a":
        return any(e.url_query for e in hit) and not enters
    if variant == "b":
        return bool(hit) and not any(e.url_query for e in results)
    if variant == "c-button":
        return any(e.ev == "button" for e in entries) and any(e.url_query for e in hit)
    if variant == "c-nobutton":
        return bool(enters) and not results
    return any(e.url_query and e.late for e in hit)


# ---- the positive controls (probe: a real Chrome, DevTools input only, no OS input) ---------


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _press_enter(cdp: CdpSession) -> None:
    for kind in ("keyDown", "keyUp"):
        cdp.call(
            "Input.dispatchKeyEvent",
            {
                "type": kind,
                "key": "Enter",
                "code": "Enter",
                "windowsVirtualKeyCode": 13,
                **({"text": "\r"} if kind == "keyDown" else {}),
            },
        )


def run_control(variant: Variant, *, slow_delay_ms: int = SLOW_DELAY_MS) -> tuple[Entry, ...]:
    """Drive one variant to its intended end state over DevTools in a throwaway profile, and
    return the page's log. (a), (b): type. (c): type, Enter, then the button when there is one.
    (d): type, Enter at once, then wait past the delay."""
    from steerai.probes.chrome_a11y import (  # noqa: PLC0415 - live probes only
        CHROME_DEFAULT,
        CdpSession,
        launch_chrome,
        page_ws_url,
        stop_chrome,
    )
    from steerai.probes.layer_b_pages import PageServer, log_value  # noqa: PLC0415

    page = spec(variant, slow_delay_ms=slow_delay_ms)
    tmp = Path(tempfile.mkdtemp(prefix="steer-instant-"))
    with PageServer(pages(slow_delay_ms=slow_delay_ms)) as server:
        port = _free_port()
        url = f"http://127.0.0.1:{server.port}{path_of(variant)}"
        proc = launch_chrome(CHROME_DEFAULT, tmp / "profile", port, url)
        try:
            cdp = CdpSession(page_ws_url(port))
            try:
                _wait_ready(cdp)
                cdp.call("Runtime.evaluate", {"expression": "document.getElementById('q').focus()"})
                cdp.call("Input.insertText", {"text": QUERY})
                if variant in {"c-button", "c-nobutton", "d"}:
                    _press_enter(cdp)
                if variant == "c-button":
                    _click_button(cdp)
                time.sleep(page.delay_ms / 1000 + SETTLE_S)
                raw = log_value(
                    cdp.call(
                        "Runtime.evaluate",
                        {
                            "expression": f"localStorage.getItem({json.dumps(LOG_KEY)}) || '[]'",
                            "returnByValue": True,
                        },
                    )
                )
            finally:
                cdp.close()
        finally:
            stop_chrome(proc, tmp / "profile")
            shutil.rmtree(tmp, ignore_errors=True)
    return parse_log(raw)


def _wait_ready(cdp: CdpSession, timeout_s: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        reply = cdp.call(
            "Runtime.evaluate",
            {"expression": "document.body && document.body.dataset.ready", "returnByValue": True},
        )
        if reply.get("result", {}).get("value") == "1":
            return
        time.sleep(0.1)
    raise TimeoutError("the fixture page never became ready")


def _click_button(cdp: CdpSession) -> None:
    reply = cdp.call(
        "Runtime.evaluate",
        {
            "expression": "(function(){var r=document.getElementById('go')"
            ".getBoundingClientRect();return [r.left+r.width/2, r.top+r.height/2];})()",
            "returnByValue": True,
        },
    )
    x, y = reply["result"]["value"]
    for kind in ("mousePressed", "mouseReleased"):
        cdp.call(
            "Input.dispatchMouseEvent",
            {"type": kind, "x": x, "y": y, "button": "left", "clickCount": 1},
        )
