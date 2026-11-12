"""The gallery's capability tokens in real Chrome (packet interaction-model-2026-11-12 §1, §5;
principal item 7: the gallery and this test are the measuring instrument). Each row is the
control type and tokens measured once (Chrome 156, 2026-11-12) and pinned here, so a Chrome that
maps an idiom differently fails here before any planner sees it. Observe only, no input."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest

from steerai.probes.chrome_a11y import CHROME_DEFAULT, launch_chrome, stop_chrome
from steerai.probes.gallery_page import page_html
from steerai.probes.layer_b_pages import PageServer
from steerai.probes.uia_snapshot import UiaClient
from steerai.probes.windows import wait_window_for_pid
from steerai.world.model import Snapshot, WorldModel

pytestmark = pytest.mark.windows

EXPECTED: dict[tuple[str, str], tuple[str, ...]] = {
    ("Button", "Save draft"): (),
    ("Button", "Bold"): ("toggle:off",),  # aria-pressed
    ("Button", "Dark mode"): ("toggle:off",),  # role=switch
    ("CheckBox", "Email me updates"): ("toggle:off",),
    ("CheckBox", "Remember this device"): ("toggle:off",),
    ("CheckBox", "Select all files"): ("toggle:mixed",),
    ("RadioButton", "Standard"): ("selected",),
    ("RadioButton", "Express"): ("selectable",),
    ("RadioButton", "A4"): ("selected",),
    ("RadioButton", "Letter"): ("selectable",),
    ("TabItem", "Profile"): ("selected",),
    ("TabItem", "Billing details"): ("selectable",),
    ("ListItem", "Apple"): ("selected",),
    ("ListItem", "Mango"): ("selectable",),
    ("ListItem", "Fiction"): ("selectable",),
    ("ComboBox", "Country"): ("collapsed",),
    ("ComboBox", "Time zone"): ("collapsed",),
    ("ComboBox", "Colour"): ("collapsed",),
    ("Button", "Shipping policy"): ("collapsed",),  # <summary>
    ("Button", "Show filters"): ("collapsed",),
    ("Button", "Share"): ("collapsed",),  # aria-haspopup=menu
    ("TreeItem", "Projects"): ("selectable", "collapsed"),
    ("DataItem", "Order 1001"): ("selectable",),
    ("Edit", "Full name"): ("editable",),
    ("Edit", "Find articles"): ("editable",),
    ("Edit", "Comments"): ("editable",),
    ("Hyperlink", "Terms of service"): (),
    ("Slider", "Volume"): ("range",),
    ("Spinner", "Quantity"): ("range",),
}


def _ready(model: WorldModel, hwnd: int, max_s: float = 25.0) -> Snapshot:
    deadline = time.monotonic() + max_s
    while True:
        snapshot = model.observe(hwnd, cap_s=5.0)
        shown = {(e.role, e.name.strip()) for e in snapshot.elements}
        if shown >= set(EXPECTED):
            return snapshot
        if time.monotonic() >= deadline:
            missing = sorted(f"{r} {n!r}" for r, n in set(EXPECTED) - shown)
            raise AssertionError(f"gallery rows never exposed: {missing}")
        time.sleep(0.5)


@pytest.mark.skipif(not CHROME_DEFAULT.is_file(), reason="Chrome is not installed")
@pytest.mark.usefixtures("warm_chrome")
def test_every_gallery_row_reads_its_measured_tokens() -> None:
    profile = Path(tempfile.mkdtemp(prefix="steer-gallery-"))
    with PageServer({"/page.html": page_html()}) as server:
        proc = launch_chrome(
            CHROME_DEFAULT,
            profile,
            9351,
            f"http://127.0.0.1:{server.port}/page.html",
            window_size=(1100, 900),
        )
        try:
            window = wait_window_for_pid(
                proc.pid, title_contains="Steer gallery", image="chrome.exe", process=proc
            )
            snapshot = _ready(WorldModel(UiaClient()), window.hwnd)
            assert snapshot.caps_read is True
            seen: dict[tuple[str, str], tuple[str, ...]] = {}
            for element in snapshot.elements:
                key = (element.role, element.name.strip())
                if key in EXPECTED:
                    seen.setdefault(key, element.caps)
            wrong = {k: (seen.get(k), v) for k, v in EXPECTED.items() if seen.get(k) != v}
            assert wrong == {}, wrong
        finally:
            stop_chrome(proc, profile)
