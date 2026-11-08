"""Probe for #148 (design call D, review of #157): how Chrome's address bar reads through UIA,
before any verifier code. Throwaway profile; no person's data.

Sections (`--section`):
- `spoof`: a local page holding `<input class="OmniboxViewViews">` (Chromium maps a web node's
  HTML class to its UIA ClassName): what FindFirst returns, how many FindAll returns, and how
  many of those have no Document above them.
- `enter`: a real tagged Enter (SendInput) after the words are put in the page's own search box,
  on the local GET form and two search sites in neither held-out set, from their home pages:
  Enter-to-omnibox-change time, the before and after values, exact query decoding.
- `omnibox`: Ctrl+L, the words typed by tagged unicode input, Enter, on the profile's default
  engine: keyboard focus and value while typing, then the after value and the focus.
- `commit` (review item 2): Enter on a URL carrying the words, typed in the omnibox after
  Ctrl+L and typed in a local GET form's box, to a local server that answers after `SLOW_S`
  and to a refused port: a 10 ms timeline of the omnibox value and focus against the server's
  request and answer times; whether the URL shows before the page answers, whether it stays
  after a failed load, and the window title then.
Every section times the full locate (FindAll by ClassName plus the walk above each element)
and the value read, and records whether "www" shows and whether an accent shows decoded.

The `enter` and `omnibox` sections inject input: the idle gate waits before every run.

Usage (Windows, the person away):
    uv run python -m steerai.probes.omnibox_read --section spoof --runs 10
    uv run python -m steerai.probes.omnibox_read --section enter --runs 10
    uv run python -m steerai.probes.omnibox_read --section omnibox --runs 10
    uv run python -m steerai.probes.omnibox_read --section commit --runs 10
"""

from __future__ import annotations

import argparse
import http.server
import json
import shutil
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.parse
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from steerai.probes.chrome_a11y import CHROME_DEFAULT, launch_chrome, page_ws_url, stop_chrome
from steerai.probes.layer_b import WINDOW, _evaluate, _free_port, _in_front, wait_until_idle
from steerai.probes.layer_b_pages import PageServer

OMNIBOX_CLASS = "OmniboxViewViews"
WORDS = ("ui automation tree", "café crème")
SITES: tuple[tuple[str, str, str, str], ...] = (
    (
        "ecosia",
        "https://www.ecosia.org/",
        "q",
        "input[name=q], input[type=search], textarea[name=q]",
    ),
    (
        "mojeek",
        "https://www.mojeek.com/",
        "q",
        "input[name=q], input[type=search], textarea[name=q]",
    ),
)
FORM = (
    '<!doctype html><title>Local search</title><form action="/results" method="get">'
    '<input name="q" aria-label="Search"><button>Go</button></form>'
)
SPOOF = (
    '<!doctype html><title>Spoof</title><input class="OmniboxViewViews" '
    'aria-label="Address and search bar" value="evil.example/search?q=ui+automation+tree">'
)
VK_RETURN, VK_CONTROL, VK_L = 0x0D, 0x11, 0x4C
SLOW_S = 2.0
TIMELINE_S = 6.0


def query_words(omnibox_value: str, param: str) -> str:
    """The words one query parameter carries in an omnibox value (scheme may be hidden),
    decoded, NFKC- and whitespace-folded, case kept."""
    value = omnibox_value if "://" in omnibox_value else "https://" + omnibox_value
    params = urllib.parse.parse_qs(urllib.parse.urlsplit(value).query)
    raw = params.get(param, [""])[0]
    return " ".join(unicodedata.normalize("NFKC", raw).split())


def shown_at(timeline: Sequence[tuple[int, str, bool]], param: str, words: str) -> int | None:
    """The first time (ms) the omnibox, out of focus, carries the words in the parameter."""
    return next(
        (
            ms
            for ms, value, focused in timeline
            if not focused and query_words(value, param) == words
        ),
        None,
    )


def _uia() -> tuple[Any, Any]:  # Any: comtypes UIA objects have no stubs
    from steerai.probes.uia_snapshot import UiaClient  # noqa: PLC0415 - Windows only

    client = UiaClient()
    return client._uia, client._uia_module


def locate(hwnd: int) -> tuple[Any, dict[str, Any]]:  # Any: a comtypes value pattern
    """FindAll by ClassName, then keep the elements with no Document above; returns the one
    kept element's value pattern (or None) and the counts and costs."""
    uia, mod = _uia()
    started = time.perf_counter()
    root = uia.ElementFromHandle(hwnd)
    found = root.FindAll(
        mod.TreeScope_Descendants,
        uia.CreatePropertyCondition(mod.UIA_ClassNamePropertyId, OMNIBOX_CLASS),
    )
    first = root.FindFirst(
        mod.TreeScope_Descendants,
        uia.CreatePropertyCondition(mod.UIA_ClassNamePropertyId, OMNIBOX_CLASS),
    )
    kept = []
    for i in range(found.Length):
        element = found.GetElement(i)
        node, document = element, False
        for _ in range(60):
            node = uia.ControlViewWalker.GetParentElement(node)
            if not node:
                break
            if node.CurrentControlType == mod.UIA_DocumentControlTypeId:
                document = True
                break
        if not document:
            kept.append(element)
    locate_ms = (time.perf_counter() - started) * 1000
    named = root.FindAll(
        mod.TreeScope_Descendants,
        uia.CreatePropertyCondition(mod.UIA_NamePropertyId, "Address and search bar"),
    ).Length  # the spoof shares the name: 2 here means the page node was in the tree
    first_in_page = bool(first) and not any(uia.CompareElements(first, k) for k in kept)
    info = {
        "found": found.Length,
        "named": named,
        "kept": len(kept),
        "first_is_page_node": first_in_page,
        "locate_ms": round(locate_ms, 2),
    }
    if len(kept) != 1:
        return None, info
    pattern = (
        kept[0]
        .GetCurrentPattern(mod.UIA_ValuePatternId)
        .QueryInterface(mod.IUIAutomationValuePattern)
    )
    return (pattern, kept[0]), info


def _value(pattern: Any) -> tuple[str, float]:  # Any: a comtypes value pattern
    started = time.perf_counter()
    value = str(pattern.CurrentValue)
    return value, round((time.perf_counter() - started) * 1000, 3)


def _format(value: str) -> dict[str, Any]:
    return {
        "www_shown": "www." in value,
        "scheme_shown": "://" in value,
        "accent_decoded": "é" in value,
    }


def _chrome(url: str) -> tuple[Any, Path, int, int]:  # Any: the Popen of Chrome
    from steerai.probes.windows import wait_window_for_pid  # noqa: PLC0415 - Windows only

    tmp = Path(tempfile.mkdtemp(prefix="steer-omni-"))
    port = _free_port()
    proc = launch_chrome(CHROME_DEFAULT, tmp / "profile", port, url, window_size=WINDOW)
    window = wait_window_for_pid(proc.pid, image="chrome.exe", process=proc)
    page_ws_url(port)
    return proc, tmp, window.hwnd, port


def _type_unicode(text: str) -> None:
    from steerai.probes import win_input  # noqa: PLC0415 - Windows only

    records = []
    for unit in text.encode("utf-16-le").hex(" ", 2).split():
        code = int.from_bytes(bytes.fromhex(unit), "little")
        records += [
            win_input.build_keyboard_unicode(code, keyup=False),
            win_input.build_keyboard_unicode(code, keyup=True),
        ]
    win_input.send_inputs(records)


def _wait_change(
    pattern: Any, before: str, limit_s: float = 10.0
) -> tuple[str, int | None]:  # Any: a comtypes value pattern
    started = time.perf_counter()
    while time.perf_counter() - started < limit_s:
        value = str(pattern.CurrentValue)
        if value != before and "?" in value:
            return value, round((time.perf_counter() - started) * 1000)
        time.sleep(0.01)
    return str(pattern.CurrentValue), None


def spoof_run(number: int) -> dict[str, Any]:
    with PageServer({"/page.html": SPOOF}) as server:
        proc, tmp, hwnd, _port = _chrome(f"http://127.0.0.1:{server.port}/page.html")
        try:
            time.sleep(1.5)
            found, info = locate(hwnd)
            value = _value(found[0])[0] if found else ""
            return {"section": "spoof", "run": number, **info, "kept_is_real": "evil" not in value}
        finally:
            stop_chrome(proc, tmp / "profile")
            shutil.rmtree(tmp, ignore_errors=True)


def enter_run(site_spec: tuple[str, str, str, str], words: str, number: int) -> dict[str, Any]:
    from steerai.probes import win_input  # noqa: PLC0415 - Windows only
    from steerai.probes.task_suite import _page  # noqa: PLC0415 - the one-tab reader

    site, home, param, selector = site_spec
    wait_until_idle()
    proc, tmp, hwnd, port = _chrome(home)
    try:
        time.sleep(4.0)  # the home page settles (a site may show a consent banner)
        _in_front(hwnd)
        found, info = locate(hwnd)
        if not found:
            return {"section": "enter", "site": site, "run": number, **info, "error": "no_omnibox"}
        pattern = found[0]
        before, read_ms = _value(pattern)
        cdp = _page(port)
        try:
            filled = _evaluate(
                cdp,
                f"(function(){{var e=document.querySelector({json.dumps(selector)});"
                "if(!e){return 'no_box';} e.focus(); return 'ok';})()",
                "fill",
            )
        finally:
            cdp.close()
        if filled != "ok":
            return {"section": "enter", "site": site, "run": number, **info, "error": filled}
        time.sleep(0.3)
        _type_unicode(words)  # real typing: a framework input ignores a value set by script
        time.sleep(0.5)
        win_input.key_tap(VK_RETURN)
        after, change_ms = _wait_change(pattern, before)
        return {
            "section": "enter",
            "site": site,
            "run": number,
            **info,
            "read_ms": read_ms,
            "before_has_words": query_words(before, param) == words,
            "change_ms": change_ms,
            "exact": query_words(after, param) == words,
            **_format(after),
        }
    finally:
        stop_chrome(proc, tmp / "profile")
        shutil.rmtree(tmp, ignore_errors=True)


def enter_local(words: str, number: int) -> dict[str, Any]:
    """The local GET form under the same real Enter (the results page carries `q`)."""
    with PageServer({"/page.html": FORM, "/results": "<title>Results</title>"}) as server:
        spec = ("local", f"http://127.0.0.1:{server.port}/page.html", "q", "input[name=q]")
        return enter_run(spec, words, number)


def omnibox_run(words: str, number: int) -> dict[str, Any]:
    from steerai.probes import win_input  # noqa: PLC0415 - Windows only

    wait_until_idle()
    proc, tmp, hwnd, _port = _chrome("about:blank")
    try:
        time.sleep(2.0)
        _in_front(hwnd)
        found, info = locate(hwnd)
        if not found:
            return {"section": "omnibox", "run": number, **info, "error": "no_omnibox"}
        pattern, element = found
        win_input.send_inputs(
            [
                win_input.build_keyboard_input(vk=VK_CONTROL, keyup=False),
                win_input.build_keyboard_input(vk=VK_L, keyup=False),
                win_input.build_keyboard_input(vk=VK_L, keyup=True),
                win_input.build_keyboard_input(vk=VK_CONTROL, keyup=True),
            ]
        )
        time.sleep(0.3)
        _type_unicode(words)
        time.sleep(0.5)
        typing_value = str(pattern.CurrentValue)
        typing_focus = bool(element.CurrentHasKeyboardFocus)
        before = typing_value
        win_input.key_tap(VK_RETURN)
        after, change_ms = _wait_change(pattern, before)
        engine = urllib.parse.urlsplit(after if "://" in after else "https://" + after).netloc
        return {
            "section": "omnibox",
            "run": number,
            **info,
            "typing_focus": typing_focus,
            "typing_value_is_words": typing_value.strip() == words,
            "typing_value_extends_words": typing_value.startswith(words) and typing_value != words,
            "after_focus": bool(element.CurrentHasKeyboardFocus),
            "change_ms": change_ms,
            "engine": engine,
            "after_is_url": "?" in after,
            "exact_q": query_words(after, "q") == words,
            **_format(after),
        }
    finally:
        stop_chrome(proc, tmp / "profile")
        shutil.rmtree(tmp, ignore_errors=True)


class _SlowServer:
    """127.0.0.1 only, GET only: `/page.html` is a GET form posting to `action` at once;
    `/results` answers after `SLOW_S`. Records when each results request arrived and when its
    answer was sent, on the probe's own perf_counter clock."""

    def __init__(self) -> None:
        self.times: list[tuple[float, float]] = []
        self.action = "/results"
        times, server = self.times, self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                arrived = time.perf_counter()
                path = self.path.split("?", 1)[0]
                if path == "/page.html":
                    self._send(FORM.replace('"/results"', json.dumps(server.action)).encode())
                elif path == "/results":
                    time.sleep(SLOW_S)
                    self._send(b"<!doctype html><title>Results</title><p>results")
                    times.append((arrived, time.perf_counter()))
                else:
                    self.send_error(404)

            def _send(self, data: bytes) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args: Any) -> None:  # noqa: ARG002 - Any: stdlib format args
                return

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def __enter__(self) -> _SlowServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()


COMMIT_TARGETS = ("bar-slow", "bar-refused", "page-slow", "page-refused")


def commit_run(target: str, words: str, number: int) -> dict[str, Any]:
    """Enter on a URL carrying the words: `bar-*` typed in the omnibox after Ctrl+L, `page-*`
    typed in a local GET form's box; `*-slow` answers after SLOW_S, `*-refused` has nothing
    listening. Then a 10 ms timeline of the omnibox value and focus, and the window title."""
    from steerai.probes import win_input  # noqa: PLC0415 - Windows only
    from steerai.probes.task_suite import _page  # noqa: PLC0415 - the one-tab reader

    with _SlowServer() as server:
        port = server.port if target.endswith("slow") else _free_port()
        server.action = f"http://127.0.0.1:{port}/results"
        typed = f"{server.action}?q={urllib.parse.quote_plus(words)}"
        on_page = target.startswith("page")
        wait_until_idle()
        start_url = f"http://127.0.0.1:{server.port}/page.html" if on_page else "about:blank"
        proc, tmp, hwnd, cdp_port = _chrome(start_url)
        try:
            time.sleep(2.0)
            _in_front(hwnd)
            found, info = locate(hwnd)
            row: dict[str, Any] = {"section": "commit", "target": target, "run": number, **info}
            if not found:
                return {**row, "error": "no_omnibox"}
            pattern, element = found
            if on_page:
                cdp = _page(cdp_port)
                try:
                    _evaluate(cdp, "document.querySelector('input[name=q]').focus(); 'ok'", "focus")
                finally:
                    cdp.close()
                time.sleep(0.3)
                _type_unicode(words)
            else:
                win_input.send_inputs(
                    [
                        win_input.build_keyboard_input(vk=VK_CONTROL, keyup=False),
                        win_input.build_keyboard_input(vk=VK_L, keyup=False),
                        win_input.build_keyboard_input(vk=VK_L, keyup=True),
                        win_input.build_keyboard_input(vk=VK_CONTROL, keyup=True),
                    ]
                )
                time.sleep(0.3)
                _type_unicode(typed)
            time.sleep(0.5)
            started = time.perf_counter()
            win_input.key_tap(VK_RETURN)
            timeline: list[tuple[int, str, bool]] = []
            while time.perf_counter() - started < TIMELINE_S:
                ms = round((time.perf_counter() - started) * 1000)
                timeline.append(
                    (ms, str(pattern.CurrentValue), bool(element.CurrentHasKeyboardFocus))
                )
                time.sleep(0.01)
            arrived, answered = server.times[0] if server.times else (None, None)
            final_value, final_focus = timeline[-1][1], timeline[-1][2]
            uia, _mod = _uia()
            return {
                **row,
                "focus_left_ms": next((ms for ms, _v, f in timeline if not f), None),
                "shown_ms": shown_at(timeline, "q", words),
                "request_ms": None if arrived is None else round((arrived - started) * 1000),
                "answer_ms": None if answered is None else round((answered - started) * 1000),
                "final_carries": query_words(final_value, "q") == words and not final_focus,
                "values": sorted({value for _ms, value, _f in timeline}),
                "title": str(uia.ElementFromHandle(hwnd).CurrentName),
            }
        finally:
            stop_chrome(proc, tmp / "profile")
            shutil.rmtree(tmp, ignore_errors=True)


def _pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="#148: Chrome's omnibox through UIA.")
    parser.add_argument("--section", choices=("spoof", "enter", "omnibox", "commit"), required=True)
    parser.add_argument("--runs", type=int, default=10)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    rows: list[dict[str, Any]] = []
    for number in range(1, args.runs + 1):
        words = WORDS[number % len(WORDS)]
        if args.section == "spoof":
            batch = [spoof_run(number)]
        elif args.section == "enter":
            batch = [enter_local(words, number)]
            batch += [enter_run(spec, words, number) for spec in SITES]
        elif args.section == "omnibox":
            batch = [omnibox_run(words, number)]
        else:
            batch = [commit_run(target, words, number) for target in COMMIT_TARGETS]
        for row in batch:
            rows.append(row)
            sys.stdout.write(json.dumps(row) + "\n")
            sys.stdout.flush()
    locates = [r["locate_ms"] for r in rows]
    sys.stdout.write(f"locate ms p50 {_pct(locates, 0.5)} p95 {_pct(locates, 0.95)}\n")
    changes = [r["change_ms"] for r in rows if r.get("change_ms") is not None]
    if changes:
        sys.stdout.write(f"change ms p50 {_pct(changes, 0.5)} p95 {_pct(changes, 0.95)}\n")
    for target in COMMIT_TARGETS:
        runs = [r for r in rows if r.get("target") == target]
        if runs:
            early = sum(
                1
                for r in runs
                if r["shown_ms"] is not None
                and (r["answer_ms"] is None or r["shown_ms"] < r["answer_ms"])
            )
            stays = sum(1 for r in runs if r["final_carries"])
            sys.stdout.write(
                f"{target}: shown before an answer {early}/{len(runs)}, "
                f"carries at {TIMELINE_S:.0f} s {stays}/{len(runs)}\n"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
