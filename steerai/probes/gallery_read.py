"""The gallery's own full read, saved for offline rating (stage 1 ruling 2026-11-15: the expected
outcomes are rated from the page's own read, never from watching a run).

Observe only: the gallery is served from memory on 127.0.0.1, opened in a throwaway Chrome
profile, read once through UIA (the snapshot plus URLs, AriaRoles, headings and landmarks, as the
v2 loop's full read takes them), and written as `world.read_json`. No input of any kind is sent
and no window is brought to the front by this probe. Only the Chrome it launched is closed, by its
PID (`stop_chrome`). The page holds only the gallery's made-up text.

Run (Windows, session unlocked, machine idle):
    uv run python -m steerai.probes.gallery_read --out tests/world/fixtures/gallery/page_read.json
"""

from __future__ import annotations

import argparse
import secrets
import shutil
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from steerai.world.model import Snapshot, normalise_name

READY_NAMES: Final = frozenset({("Edit", "Find articles"), ("Hyperlink", "Terms of service")})
"""The gallery's last rows: the read is taken once Chrome exposes them."""
READY_S: Final = 25.0
POLL_S: Final = 0.5


def ready(snapshot: Snapshot) -> bool:
    """Chrome exposes the gallery's last rows."""
    shown = {(e.role, normalise_name(e.name)) for e in snapshot.elements}
    return shown >= READY_NAMES


def capture(out: Path) -> int:  # pragma: no cover - live, Windows only
    from steerai.probes.chrome_a11y import (  # noqa: PLC0415 - live probes only
        CHROME_DEFAULT,
        launch_chrome,
        stop_chrome,
    )
    from steerai.probes.gallery_page import pages  # noqa: PLC0415
    from steerai.probes.layer_b import WINDOW, _free_port  # noqa: PLC0415
    from steerai.probes.layer_b_pages import PageServer  # noqa: PLC0415
    from steerai.probes.shadow_v2 import _ComFacts  # noqa: PLC0415 - COM failures as OSError
    from steerai.probes.uia_snapshot import UiaClient  # noqa: PLC0415
    from steerai.probes.windows import list_top_windows, wait_window_for_pid  # noqa: PLC0415
    from steerai.world.model import WorldModel  # noqa: PLC0415
    from steerai.world.observe2 import read_observed  # noqa: PLC0415
    from steerai.world.read_json import observed_to_json  # noqa: PLC0415

    tmp = Path(tempfile.mkdtemp(prefix="steer-gallery-read-"))
    with PageServer(pages()) as server:
        proc = launch_chrome(
            CHROME_DEFAULT,
            tmp / "profile",
            _free_port(),
            f"http://127.0.0.1:{server.port}/page.html",
            window_size=WINDOW,
        )
        try:
            window = wait_window_for_pid(
                proc.pid, title_contains="Steer gallery", image="chrome.exe", process=proc
            )
            client = UiaClient()
            model = WorldModel(client)
            deadline = time.monotonic() + READY_S
            snapshot = model.observe(window.hwnd, cap_s=5.0)
            while not ready(snapshot):
                if time.monotonic() >= deadline:
                    sys.stdout.write("gallery_read: the gallery's rows never showed\n")
                    return 1
                time.sleep(POLL_S)
                snapshot = model.observe(window.hwnd, cap_s=5.0)
            read = read_observed(
                snapshot,
                _ComFacts(client),
                key=secrets.token_bytes(32),
                windows=lambda: tuple(
                    sorted(w.hwnd for w in list_top_windows() if w.pid == proc.pid)
                ),
            )
        finally:
            stop_chrome(proc, tmp / "profile")
            shutil.rmtree(tmp, ignore_errors=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes((observed_to_json(read) + "\n").encode("utf-8"))
    roles = "unread" if read.aria_roles is None else str(len(read.aria_roles))
    marks = "unread" if read.landmarks is None else str(len(read.landmarks))
    sys.stdout.write(
        f"gallery_read: {len(snapshot.elements)} elements, roles {roles}, landmarks {marks}\n"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - live
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--out", type=Path, required=True, help="where the read is written")
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        sys.stdout.write("gallery_read: windows only\n")
        return 0
    return capture(args.out)


if __name__ == "__main__":  # pragma: no cover - live
    raise SystemExit(main())
