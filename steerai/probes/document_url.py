"""Probe for the [v2.48] load signal (D-151 principal REVISE, blocking 2): what Chrome's
top-level Document exposes through UIA (its name, and its ValuePattern value, which Chromium
fills with the document's URL) on results pages, error pages, a certificate interstitial, a
local 404 and during an omnibox refinement, and what the read costs. Throwaway profiles.

Sections (`--section`):
- `pages`: each case loaded by URL, no input; the top Document's name and value after load.
- `refine`: from a local results page for "ui automation tree", Ctrl+L, a URL for the shorter
  query "ui automation" to a server that answers after `SLOW_S`, Enter (tagged input,
  idle-gated); the top Document's name and value while the answer is pending and after it.

Usage (Windows):
    uv run python -m steerai.probes.document_url --section pages --runs 5
    uv run python -m steerai.probes.document_url --section refine --runs 5
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
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from steerai.probes.chrome_a11y import CHROME_DEFAULT, launch_chrome, page_ws_url, stop_chrome
from steerai.probes.layer_b import WINDOW, _free_port, _in_front, wait_until_idle
from steerai.probes.layer_b_pages import PageServer

SLOW_S = 2.0
REMOTE = (
    ("mdn", "https://developer.mozilla.org/en-US/search?q=flexbox"),
    ("pydocs", "https://docs.python.org/3/search.html?q=pathlib"),
    ("google", "https://www.google.com/search?q=ui+automation+tree"),
    ("bing", "https://www.bing.com/search?q=ui+automation+tree"),
    ("ddg", "https://duckduckgo.com/?q=ui+automation+tree"),
    ("cert", "https://self-signed.badssl.com/"),
)
VK_RETURN, VK_CONTROL, VK_L = 0x0D, 0x11, 0x4C


def scheme_of(value: str) -> str:
    """The scheme of a Document value ("" when it carries none)."""
    head, sep, _rest = value.partition("://")
    return head if sep and head.isascii() and head.replace("-", "").isalnum() else ""


def _uia() -> tuple[Any, Any]:  # Any: comtypes UIA objects have no stubs
    from steerai.probes.uia_snapshot import UiaClient  # noqa: PLC0415 - Windows only

    client = UiaClient()
    return client._uia, client._uia_module


def top_documents(hwnd: int) -> tuple[list[dict[str, str]], float]:
    """Every Document with no Document above it: name and ValuePattern value; and the cost."""
    uia, mod = _uia()
    started = time.perf_counter()
    root = uia.ElementFromHandle(hwnd)
    found = root.FindAll(
        mod.TreeScope_Descendants,
        uia.CreatePropertyCondition(mod.UIA_ControlTypePropertyId, mod.UIA_DocumentControlTypeId),
    )
    docs = []
    for i in range(found.Length):
        element = found.GetElement(i)
        node, nested = element, False
        for _ in range(60):
            node = uia.ControlViewWalker.GetParentElement(node)
            if not node:
                break
            if node.CurrentControlType == mod.UIA_DocumentControlTypeId:
                nested = True
                break
        if nested:
            continue
        pattern = element.GetCurrentPattern(mod.UIA_ValuePatternId)
        value = (
            str(pattern.QueryInterface(mod.IUIAutomationValuePattern).CurrentValue)
            if pattern
            else "<no ValuePattern>"
        )
        docs.append({"name": str(element.CurrentName), "value": value})
    return docs, round((time.perf_counter() - started) * 1000, 2)


def _chrome(url: str) -> tuple[Any, Path, int]:  # Any: the Popen of Chrome
    from steerai.probes.windows import wait_window_for_pid  # noqa: PLC0415 - Windows only

    tmp = Path(tempfile.mkdtemp(prefix="steer-docurl-"))
    port = _free_port()
    proc = launch_chrome(CHROME_DEFAULT, tmp / "profile", port, url, window_size=WINDOW)
    window = wait_window_for_pid(proc.pid, image="chrome.exe", process=proc)
    page_ws_url(port)
    return proc, tmp, window.hwnd


def _warm(hwnd: int) -> None:
    from steerai.probes.uia_snapshot import UiaClient  # noqa: PLC0415 - Windows only
    from steerai.run import warm_up  # noqa: PLC0415
    from steerai.world.model import WorldModel  # noqa: PLC0415

    model = WorldModel(UiaClient())
    warm_up(lambda: model.observe(hwnd), cap_s=15.0)


def page_case(case: str, url: str, number: int) -> dict[str, Any]:
    proc, tmp, hwnd = _chrome(url)
    try:
        time.sleep(5.0)
        _warm(hwnd)
        docs, read_ms = top_documents(hwnd)
        return {
            "section": "pages",
            "case": case,
            "run": number,
            "docs": docs,
            "schemes": [scheme_of(d["value"]) for d in docs],
            "read_ms": read_ms,
        }
    finally:
        stop_chrome(proc, tmp / "profile")
        shutil.rmtree(tmp, ignore_errors=True)


def local_cases(number: int) -> list[dict[str, Any]]:
    """A refused port, and a local 404 whose page has its own title."""
    rows = [page_case("refused", f"http://127.0.0.1:{_free_port()}/results?q=flexbox", number)]
    with PageServer({"/page.html": "<title>Local</title>"}) as server:
        rows.append(
            page_case("http404", f"http://127.0.0.1:{server.port}/missing?q=flexbox", number)
        )
    return rows


class _Slow:
    """`/results` answers after SLOW_S, titled with the query it carries."""

    def __init__(self) -> None:
        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                query = self.path.partition("q=")[2].replace("+", " ")
                if not self.path.startswith("/results"):
                    self.send_error(404)
                    return
                if query != "ui automation tree":
                    time.sleep(SLOW_S)
                data = f"<!doctype html><title>{query} - Local search</title><p>r".encode()
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

    def __enter__(self) -> _Slow:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()


def refine_case(number: int) -> dict[str, Any]:
    from steerai.probes import win_input  # noqa: PLC0415 - Windows only
    from steerai.probes.omnibox_read import _type_unicode  # noqa: PLC0415

    with _Slow() as server:
        wait_until_idle()
        proc, tmp, hwnd = _chrome(f"http://127.0.0.1:{server.port}/results?q=ui+automation+tree")
        try:
            time.sleep(2.0)
            _in_front(hwnd)
            _warm(hwnd)
            before, _ = top_documents(hwnd)
            win_input.send_inputs(
                [
                    win_input.build_keyboard_input(vk=VK_CONTROL, keyup=False),
                    win_input.build_keyboard_input(vk=VK_L, keyup=False),
                    win_input.build_keyboard_input(vk=VK_L, keyup=True),
                    win_input.build_keyboard_input(vk=VK_CONTROL, keyup=True),
                ]
            )
            time.sleep(0.3)
            _type_unicode(f"http://127.0.0.1:{server.port}/results?q=ui+automation")
            time.sleep(0.5)
            win_input.key_tap(VK_RETURN)
            time.sleep(0.5)
            pending, _ = top_documents(hwnd)
            time.sleep(SLOW_S + 1.5)
            after, read_ms = top_documents(hwnd)
            return {
                "section": "refine",
                "run": number,
                "before": before,
                "pending": pending,
                "after": after,
                "read_ms": read_ms,
            }
        finally:
            stop_chrome(proc, tmp / "profile")
            shutil.rmtree(tmp, ignore_errors=True)


def _pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="The top Document's URL through UIA.")
    parser.add_argument("--section", choices=("pages", "refine"), required=True)
    parser.add_argument("--runs", type=int, default=5)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    rows: list[dict[str, Any]] = []
    for number in range(1, args.runs + 1):
        if args.section == "pages":
            batch = [page_case(case, url, number) for case, url in REMOTE]
            batch += local_cases(number)
        else:
            batch = [refine_case(number)]
        for row in batch:
            rows.append(row)
            sys.stdout.write(json.dumps(row, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    reads = [r["read_ms"] for r in rows]
    sys.stdout.write(f"read ms p50 {_pct(reads, 0.5)} p95 {_pct(reads, 0.95)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
