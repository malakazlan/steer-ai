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
Every section times the full locate (FindAll by ClassName plus the walk above each element)
and the value read, and records whether "www" shows and whether an accent shows decoded.

The `enter` and `omnibox` sections inject input: the idle gate waits before every run.

Usage (Windows, the person away):
    uv run python -m steerai.probes.omnibox_read --section spoof --runs 10
    uv run python -m steerai.probes.omnibox_read --section enter --runs 10
    uv run python -m steerai.probes.omnibox_read --section omnibox --runs 10
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
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


def query_words(omnibox_value: str, param: str) -> str:
    """The words one query parameter carries in an omnibox value (scheme may be hidden),
    decoded, NFKC- and whitespace-folded, case kept."""
    value = omnibox_value if "://" in omnibox_value else "https://" + omnibox_value
    params = urllib.parse.parse_qs(urllib.parse.urlsplit(value).query)
    raw = params.get(param, [""])[0]
    return " ".join(unicodedata.normalize("NFKC", raw).split())


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


def _pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="#148: Chrome's omnibox through UIA.")
    parser.add_argument("--section", choices=("spoof", "enter", "omnibox"), required=True)
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
        else:
            batch = [omnibox_run(words, number)]
        for row in batch:
            rows.append(row)
            sys.stdout.write(json.dumps(row) + "\n")
            sys.stdout.flush()
    locates = [r["locate_ms"] for r in rows]
    sys.stdout.write(f"locate ms p50 {_pct(locates, 0.5)} p95 {_pct(locates, 0.95)}\n")
    changes = [r["change_ms"] for r in rows if r.get("change_ms") is not None]
    if changes:
        sys.stdout.write(f"change ms p50 {_pct(changes, 0.5)} p95 {_pct(changes, 0.95)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
