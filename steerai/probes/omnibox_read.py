"""Probe for #148 (design call D): how Chrome's address bar reads through UIA, before any
verifier code. Throwaway profile, no OS input (CDP drives the pages), no person's data.

Measured per run: the omnibox found by its ClassName (never the localized name, never the
AutomationId, which is a per-build view id), no Document above it, its value before and after,
the time from a GET form submit to the value change (the local form), the value read cost, and
whether the query parameter decodes back to the typed words exactly (local form and four GET
search sites in neither held-out set).

Usage (Windows):
    uv run python -m steerai.probes.omnibox_read --runs 10
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
import urllib.parse
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from steerai.probes.chrome_a11y import CHROME_DEFAULT, launch_chrome, page_ws_url, stop_chrome
from steerai.probes.layer_b import WINDOW, _evaluate, _free_port
from steerai.probes.layer_b_pages import PageServer

OMNIBOX_CLASS = "OmniboxViewViews"
WORDS = ("ui automation tree", "café crème")
SITES: tuple[tuple[str, str, str], ...] = (
    ("ecosia", "https://www.ecosia.org/search", "q"),
    ("brave", "https://search.brave.com/search", "q"),
    ("yahoo", "https://search.yahoo.com/search", "p"),
    ("mojeek", "https://www.mojeek.com/search", "q"),
)
FORM = (
    '<!doctype html><title>Local search</title><form action="/results" method="get">'
    '<input name="q" aria-label="Search"><button>Go</button></form>'
)


def query_words(omnibox_value: str, param: str) -> str:
    """The words one query parameter carries in an omnibox value (scheme may be hidden),
    decoded, NFKC- and whitespace-folded, case kept."""
    import unicodedata  # noqa: PLC0415 - only here

    value = omnibox_value if "://" in omnibox_value else "https://" + omnibox_value
    params = urllib.parse.parse_qs(urllib.parse.urlsplit(value).query)
    raw = params.get(param, [""])[0]
    return " ".join(unicodedata.normalize("NFKC", raw).split())


def _omnibox(hwnd: int) -> tuple[Any, bool]:
    """The omnibox value pattern and whether a Document sits above the omnibox."""
    from steerai.probes.uia_snapshot import UiaClient  # noqa: PLC0415 - Windows only

    client = UiaClient()
    uia, mod = client._uia, client._uia_module
    root = uia.ElementFromHandle(hwnd)
    found = root.FindFirst(
        mod.TreeScope_Descendants,
        uia.CreatePropertyCondition(mod.UIA_ClassNamePropertyId, OMNIBOX_CLASS),
    )
    if not found:
        raise LookupError("no omnibox")
    node, above = found, []
    for _ in range(40):
        node = uia.ControlViewWalker.GetParentElement(node)
        if not node:
            break
        above.append(node.CurrentControlType)
    pattern = found.GetCurrentPattern(mod.UIA_ValuePatternId).QueryInterface(
        mod.IUIAutomationValuePattern
    )
    return pattern, mod.UIA_DocumentControlTypeId in above


def _chrome(url: str) -> tuple[Any, Path, int, int]:
    from steerai.probes.windows import wait_window_for_pid  # noqa: PLC0415 - Windows only

    tmp = Path(tempfile.mkdtemp(prefix="steer-omni-"))
    port = _free_port()
    proc = launch_chrome(CHROME_DEFAULT, tmp / "profile", port, url, window_size=WINDOW)
    window = wait_window_for_pid(proc.pid, image="chrome.exe", process=proc)
    page_ws_url(port)
    return proc, tmp, window.hwnd, port


def local_run(words: str) -> dict[str, Any]:
    from steerai.probes.task_suite import _page  # noqa: PLC0415 - the one-tab reader

    with PageServer({"/page.html": FORM, "/results": "<title>Results</title>"}) as server:
        proc, tmp, hwnd, port = _chrome(f"http://127.0.0.1:{server.port}/page.html")
        try:
            time.sleep(1.5)
            pattern, doc_above = _omnibox(hwnd)
            before = str(pattern.CurrentValue)
            cdp = _page(port)
            _evaluate(
                cdp, f"document.querySelector('input').value={json.dumps(words)}; 'ok'", "fill"
            )
            started = time.perf_counter()
            _evaluate(cdp, "document.querySelector('form').submit(); 'ok'", "submit")
            cdp.close()
            after, change_ms = before, None
            while time.perf_counter() - started < 5.0:
                after = str(pattern.CurrentValue)
                if after != before:
                    change_ms = round((time.perf_counter() - started) * 1000)
                    break
                time.sleep(0.01)
            reads = []
            for _ in range(20):
                t0 = time.perf_counter()
                pattern.CurrentValue  # noqa: B018 - the read being timed
                reads.append((time.perf_counter() - t0) * 1000)
            return {
                "site": "local",
                "words": words,
                "doc_above": doc_above,
                "before_has_words": query_words(before, "q") == words,
                "change_ms": change_ms,
                "exact": query_words(after, "q") == words,
                "read_ms_p50": round(sorted(reads)[10], 2),
            }
        finally:
            stop_chrome(proc, tmp / "profile")
            shutil.rmtree(tmp, ignore_errors=True)


def site_run(site: str, base: str, param: str, words: str) -> dict[str, Any]:
    url = base + "?" + urllib.parse.urlencode({param: words})
    proc, tmp, hwnd, _port = _chrome(url)
    try:
        time.sleep(4.0)  # the site settles and any redirect lands
        pattern, doc_above = _omnibox(hwnd)
        value = str(pattern.CurrentValue)
        return {
            "site": site,
            "words": words,
            "doc_above": doc_above,
            "scheme_shown": "://" in value,
            "exact": query_words(value, param) == words,
        }
    finally:
        stop_chrome(proc, tmp / "profile")
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="#148: Chrome's omnibox through UIA.")
    parser.add_argument("--runs", type=int, default=10)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    rows: list[dict[str, Any]] = []
    for number in range(args.runs):
        words = WORDS[number % len(WORDS)]
        rows.append(local_run(words))
        rows.extend(site_run(site, base, param, words) for site, base, param in SITES)
        for row in rows[-1 - len(SITES) :]:
            sys.stdout.write(json.dumps(row, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    for site in ["local", *(s for s, _, _ in SITES)]:
        mine = [r for r in rows if r["site"] == site]
        exact = sum(r["exact"] for r in mine)
        above = sum(r["doc_above"] for r in mine)
        line = f"{site}: exact {exact}/{len(mine)}, document above {above}"
        if site == "local":
            changes = sorted(r["change_ms"] for r in mine if r["change_ms"] is not None)
            reads = sorted(r["read_ms_p50"] for r in mine)
            line += (
                f", submit->change ms {changes[0]}-{changes[-1]} p50 {changes[len(changes) // 2]}"
                f", read p50 {reads[len(reads) // 2]} ms"
                if changes
                else ", no change seen"
            )
        sys.stdout.write(line + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
