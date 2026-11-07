"""#89 probe: do a YouTube search box and its result links keep their UIA RuntimeId over a hold
with no input? Each run uses a fresh test Chrome profile. Read-only: no input is sent. Prints
counts only, never element names.

Usage (Windows, session unlocked):
    uv run python -m steerai.probes.runtime_id_probe --runs 5 --hold 10
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from steerai.probes.chrome_a11y import CHROME_DEFAULT, launch_chrome, stop_chrome
from steerai.probes.uia_snapshot import UiaClient
from steerai.probes.windows import session_locked, wait_window_for_pid
from steerai.world.elements import Element
from steerai.world.model import Snapshot, WorldModel

URL = "https://www.youtube.com/results?search_query=drake+latest+video"
WORD = "drake"
PORT = 9367
MAX_LINKS = 10
READY_S = 30.0


@dataclass(frozen=True, slots=True)
class Kept:
    box_same: int
    box_n: int
    link_same: int
    link_n: int


def _is_box(element: Element) -> bool:
    return element.role in ("Edit", "ComboBox") and element.name.lower().startswith("search")


def _is_link(element: Element, word: str) -> bool:
    return element.role == "Hyperlink" and word in element.name.lower()


def kept_ids(first: Sequence[Element], second: Sequence[Element], *, word: str) -> Kept:
    """How many search boxes and result links (at most ten) of the first look carry the same
    RuntimeId under the same role and name in the second; one gone counts as changed."""
    after = {(e.role, e.name): e.runtime_id for e in second}
    before = {(e.role, e.name): e for e in first}
    boxes = [e for e in before.values() if _is_box(e)]
    links = [e for e in before.values() if _is_link(e, word)][:MAX_LINKS]
    return Kept(
        box_same=sum(1 for e in boxes if after.get((e.role, e.name)) == e.runtime_id),
        box_n=len(boxes),
        link_same=sum(1 for e in links if after.get((e.role, e.name)) == e.runtime_id),
        link_n=len(links),
    )


def _ready(model: WorldModel, hwnd: int) -> Snapshot:
    deadline = time.monotonic() + READY_S
    while True:
        snap = model.observe(hwnd, cap_s=5.0)
        if any(_is_box(e) for e in snap.elements) and any(_is_link(e, WORD) for e in snap.elements):
            return snap
        if time.monotonic() > deadline:
            raise TimeoutError("the results page did not expose its search box and links")
        time.sleep(1.0)


def _run(model: WorldModel, hold_s: float) -> Kept:
    profile = Path(tempfile.mkdtemp(prefix="steer-rid-"))
    proc = launch_chrome(CHROME_DEFAULT, profile, PORT, URL, window_size=(1000, 700))
    try:
        window = wait_window_for_pid(
            proc.pid, title_contains="Google Chrome", image="chrome.exe", process=proc
        )
        first = _ready(model, window.hwnd)
        time.sleep(hold_s)
        second = model.observe(window.hwnd, cap_s=5.0)
        return kept_ids(first.elements, second.elements, word=WORD)
    finally:
        stop_chrome(proc, profile)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RuntimeId stability over a hold with no input.")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--hold", type=float, default=10.0, help="seconds between the two looks")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if session_locked() is not False:
        sys.stderr.write("session locked or unknown: no run\n")
        return 3
    model = WorldModel(UiaClient())
    box_same = box_n = link_same = link_n = 0
    for run in range(args.runs):
        kept = _run(model, args.hold)
        box_same += kept.box_same
        box_n += kept.box_n
        link_same += kept.link_same
        link_n += kept.link_n
        sys.stdout.write(
            f"run {run + 1}: search box {kept.box_same}/{kept.box_n} same RuntimeId, "
            f"result links {kept.link_same}/{kept.link_n}\n"
        )
    sys.stdout.write(f"total: search box {box_same}/{box_n}, result links {link_same}/{link_n}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
