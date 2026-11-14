"""One throwaway Chrome on one URL for the live-check probes: the pre-flight, the one-browser slot,
a window, a DevTools session and the UIA objects, all released however the body ends.

Every launch goes through `guarded_launch`, so a low-memory machine refuses with `LowMemory`
before Chrome starts and a second browser in the process is `BrowserBusyError`. Windows only.
"""

from __future__ import annotations

import shutil
import tempfile
import time
from collections.abc import Iterator
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


@dataclass(frozen=True, slots=True)
class LivePage:
    hwnd: int
    cdp: CdpSession
    client: Any  # Any: steerai.probes.uia_snapshot.UiaClient, imported lazily (Windows only)
    uia: Any  # Any: the IUIAutomation COM object (no stubs)
    mod: Any  # Any: the generated UIAutomationClient module (no stubs)


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
        yield LivePage(hwnd, cdp, client, client._uia, client._uia_module)
    finally:
        if cdp is not None:
            cdp.close()
        guarded_stop(stop_chrome, proc, tmp / "profile")
        shutil.rmtree(tmp, ignore_errors=True)
