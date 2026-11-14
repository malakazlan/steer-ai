"""One throwaway Chrome on one URL for the live-check probes: the pre-flight, the one-browser slot,
a window, a DevTools session and the UIA objects, all released however the body ends.

Every launch goes through `guarded_launch`, so a low-memory machine refuses with `LowMemory`
before Chrome starts and a second browser in the process is `BrowserBusyError`. Windows only.
"""

from __future__ import annotations

import shutil
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from steerai.probes.chrome_a11y import (
    CHROME_DEFAULT,
    CdpSession,
    launch_chrome,
    page_ws_url,
    stop_chrome,
)
from steerai.probes.layer_b import _free_port
from steerai.probes.memory_guard import guarded_launch, guarded_stop

READY_WAIT_S = 3.0
"""Unmeasured: the wait after the window exists for the page's first layout."""
TREE_DEADLINE_S = 20.0
TREE_POLL_S = 0.5
"""Live run 1: the first UIA touch of a fresh Chrome sees an accessibility tree that is not
built yet (empty roles, no Document, elements not found). Reads retry within this bounded
deadline (a guard, unmeasured), then fail loudly."""


class TreeNotReady(RuntimeError):  # noqa: N818 - a probe outcome, named for its error row
    """The page's tree never became readable within the deadline; `str` is the error token."""


def retry_until[T](  # noqa: PLR0913 - the read, its label and the injected clock
    read: Callable[[], T | None],
    *,
    what: str,
    deadline_s: float = TREE_DEADLINE_S,
    poll_s: float = TREE_POLL_S,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """`read()` until it gives something non-empty, within `deadline_s`; else TreeNotReady(what).
    A first read of a fresh page is never taken as the answer just because it came first."""
    deadline = clock() + deadline_s
    while True:
        got = read()
        if got:
            return got
        if clock() >= deadline:
            raise TreeNotReady(what)
        sleep(poll_s)


def exit_code(rows: Sequence[Mapping[str, Any]]) -> int:  # Any: JSON rows
    """A probe that read nothing fails loudly: 1 when any row is an error or a missing element,
    never "exit 0 with empty data"."""
    failed = any("error" in r or r.get("found") is False for r in rows)
    return 1 if failed else 0


def top_document(uia: Any, mod: Any, hwnd: int) -> Any:  # pragma: no cover - live; Any: COM
    """The window's first Document, only once it has a descendant; None before that."""
    document = uia.ElementFromHandle(hwnd).FindFirst(
        mod.TreeScope_Descendants,
        uia.CreatePropertyCondition(mod.UIA_ControlTypePropertyId, mod.UIA_DocumentControlTypeId),
    )
    if not document:
        return None
    child = document.FindFirst(mod.TreeScope_Children, uia.CreateTrueCondition())
    return document if child else None


def find_in_document(page: LivePage, automation_id: str) -> Any:  # pragma: no cover - live
    """The element with `automation_id` inside the page's Document, retried while the tree
    builds; TreeNotReady(`not_found:<id>`) at the deadline."""
    mod, uia = page.mod, page.uia
    condition = uia.CreatePropertyCondition(mod.UIA_AutomationIdPropertyId, automation_id)
    return retry_until(
        lambda: page.document.FindFirst(mod.TreeScope_Descendants, condition) or None,
        what=f"not_found:{automation_id}",
    )


@dataclass(frozen=True, slots=True)
class LivePage:
    hwnd: int
    cdp: CdpSession
    client: Any  # Any: steerai.probes.uia_snapshot.UiaClient, imported lazily (Windows only)
    uia: Any  # Any: the IUIAutomation COM object (no stubs)
    mod: Any  # Any: the generated UIAutomationClient module (no stubs)
    document: Any  # Any: the page's top Document, once it has descendants (COM)


@contextmanager
def live_page(  # pragma: no cover - live
    url: str, *, floor_mb: int, settle_s: float = READY_WAIT_S
) -> Iterator[LivePage]:
    """Chrome on `url` (a throwaway profile), its window, DevTools and UIA; everything is
    stopped and the profile removed on exit."""
    from steerai.probes.uia_snapshot import UiaClient  # noqa: PLC0415 - Windows only
    from steerai.probes.windows import wait_window_for_pid  # noqa: PLC0415 - Windows only

    tmp = Path(tempfile.mkdtemp(prefix="steer-live-"))
    port = _free_port()
    proc = guarded_launch(
        launch_chrome, CHROME_DEFAULT, tmp / "profile", port, url, floor_mb=floor_mb
    )
    cdp: CdpSession | None = None
    try:
        hwnd = wait_window_for_pid(proc.pid, image="chrome.exe", process=proc).hwnd
        cdp = CdpSession(page_ws_url(port))
        time.sleep(settle_s)
        client = UiaClient()
        uia, mod = client._uia, client._uia_module
        document = retry_until(lambda: top_document(uia, mod, hwnd), what="no_document")
        yield LivePage(hwnd, cdp, client, uia, mod, document)
    finally:
        if cdp is not None:
            cdp.close()
        guarded_stop(stop_chrome, proc, tmp / "profile")
        shutil.rmtree(tmp, ignore_errors=True)
