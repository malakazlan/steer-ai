"""[V2.62] probes before the cover check relies on them: what Chrome exposes to UIA when its window
is covered, partly covered, in the background but visible, or minimised, and whether native
window occlusion is the mechanism.

Chrome's native occlusion tracking marks a window occluded only when other windows cover its
whole rectangle, and then stops rendering its tab like a background tab; minimised windows are
marked hidden (Chromium `docs/windows_native_window_occlusion_tracking.md`; the feature is
`CalculateNativeWinOcclusion` in `ui/base/ui_base_features.cc`).

Cases, each in fresh throwaway Chrome profiles serving the 3d page `/a` on 127.0.0.1:
- `front`: the target alone;
- `covered_full`: a second own Chrome placed exactly over the target;
- `covered_partial`: a second own Chrome over the target's left half;
- `background_visible`: the target in the work area's left half, a second own Chrome in the
  right half (the target is not the foreground, and nothing covers it);
- `minimized`: the target minimised without activation;
- `covered_after_read`: the target read until its Document shows, then covered fully;
- `covered_full_no_occlusion`: `covered_full` with the target launched with
  `--disable-features=CalculateNativeWinOcclusion` (diagnostic only; never in a scored run).

The only windows arranged are this probe's own Chromes (`SetWindowPos`, `ShowWindow`, no
activation); the person's windows are never moved or touched. No keyboard or mouse input. Each
case reads the target every 250 ms for 10 s (`FIRST_READY_S`) with v2's world observe, and logs
whether the target is the foreground, minimised, cloaked, and how much of it the windows above it
in z-order cover. Only the Chromes this probe launched are closed, by their PIDs.

Run (Windows, session unlocked, machine idle, at least 4 GB free):
    uv run python -m steerai.probes.occlusion --runs 5 --out <file.jsonl>
"""

from __future__ import annotations

import argparse
import ctypes
import json
import shutil
import sys
import tempfile
import time
from collections.abc import Sequence
from ctypes import wintypes
from pathlib import Path
from typing import Any, Final

from steerai.world.cover import covered_fraction

Rect = tuple[int, int, int, int]

CASES: Final = (
    "front",
    "covered_full",
    "covered_partial",
    "background_visible",
    "minimized",
    "covered_after_read",
    "covered_full_no_occlusion",
)
NO_OCCLUSION: Final = "--disable-features=CalculateNativeWinOcclusion"
SAMPLE_S: Final = 0.25
SPAN_S: Final = 10.0
SETTLE_S: Final = 0.5
"""After an arrangement, before the first read (unmeasured: the occlusion tracker's own pass)."""


def first_page(samples: Sequence[dict[str, Any]]) -> float | None:  # Any: JSON
    """The first sample time at which the top Document had something under it."""
    for sample in samples:
        inside = sample.get("doc_nodes")
        if isinstance(inside, int) and inside > 0:
            return float(sample["t"])
    return None


# ---- Windows calls on this probe's own windows (live) ---------------------------------


def _user32() -> Any:  # pragma: no cover - live; Any: ctypes.WinDLL
    return ctypes.WinDLL("user32", use_last_error=True)


def _frame(hwnd: int) -> Rect:  # pragma: no cover - live, Windows only
    """The visible frame (DWMWA_EXTENDED_FRAME_BOUNDS, 9): without the invisible resize border,
    so side-by-side windows do not overlap by their shadows."""
    rect = wintypes.RECT()
    result = ctypes.windll.dwmapi.DwmGetWindowAttribute(
        wintypes.HWND(hwnd), 9, ctypes.byref(rect), ctypes.sizeof(rect)
    )
    if result != 0:
        _user32().GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect))
    return (rect.left, rect.top, rect.right, rect.bottom)


def _window_rect(hwnd: int) -> Rect:  # pragma: no cover - live, Windows only
    """GetWindowRect: the frame plus the invisible resize border."""
    rect = wintypes.RECT()
    _user32().GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect))
    return (rect.left, rect.top, rect.right, rect.bottom)


def _cloaked(hwnd: int) -> bool:  # pragma: no cover - live, Windows only
    value = ctypes.c_int(0)
    result = ctypes.windll.dwmapi.DwmGetWindowAttribute(
        wintypes.HWND(hwnd), 14, ctypes.byref(value), ctypes.sizeof(value)
    )
    return result == 0 and value.value != 0


def _above(hwnd: int, rect_of: Any) -> list[Rect]:  # pragma: no cover - live; Any: a reader
    """`rect_of` (frame or window rect) of the visible, uncloaked, non-minimised top-level
    windows above `hwnd` in z-order (GW_HWNDPREV, 3)."""
    user32 = _user32()
    user32.GetWindow.restype = wintypes.HWND
    found: list[Rect] = []
    current = user32.GetWindow(wintypes.HWND(hwnd), 3)
    while current:
        handle = int(current)
        shown = user32.IsWindowVisible(current) and not user32.IsIconic(current)
        if shown and not _cloaked(handle):
            rect = rect_of(handle)
            if rect[2] > rect[0] and rect[3] > rect[1]:
                found.append(rect)
        current = user32.GetWindow(current, 3)
    return found


def _place(hwnd: int, rect: Rect) -> None:  # pragma: no cover - live, Windows only
    """Move and size one of this probe's own windows, without activating it or changing its
    z-order (SWP_NOZORDER 0x4 | SWP_NOACTIVATE 0x10)."""
    left, top, right, bottom = rect
    if not _user32().SetWindowPos(
        wintypes.HWND(hwnd), None, left, top, right - left, bottom - top, 0x4 | 0x10
    ):
        raise OSError("SetWindowPos refused")


def _minimise(hwnd: int) -> None:  # pragma: no cover - live, Windows only
    _user32().ShowWindow(wintypes.HWND(hwnd), 7)  # SW_SHOWMINNOACTIVE


def _work_area() -> Rect:  # pragma: no cover - live, Windows only
    rect = wintypes.RECT()
    _user32().SystemParametersInfoW(0x30, 0, ctypes.byref(rect), 0)  # SPI_GETWORKAREA
    return (rect.left, rect.top, rect.right, rect.bottom)


def _state(hwnd: int) -> dict[str, Any]:  # pragma: no cover - live; Any: JSON
    user32 = _user32()
    frame = _frame(hwnd)
    return {
        "foreground": int(user32.GetForegroundWindow() or 0) == hwnd,
        "minimized": bool(user32.IsIconic(wintypes.HWND(hwnd))),
        "cloaked": _cloaked(hwnd),
        "covered_frames": round(covered_fraction(frame, _above(hwnd, _frame)), 4),
        "covered_rects": round(covered_fraction(_window_rect(hwnd), _above(hwnd, _window_rect)), 4),
    }


# ---- one case -----------------------------------------------------------------------------


def run_case(  # noqa: PLR0915 - one case  # pragma: no cover - live
    case: str, number: int, *, client_first: bool
) -> dict[str, Any]:
    """One case. `client_first`: this process's UIA client exists before the target Chrome
    starts (it never does in the stage 1 harness, whose parent asks UIA nothing); otherwise the
    client is created only at the first read, after the arrangement."""
    from steerai.probes import instant_search as fixture  # noqa: PLC0415 - live only
    from steerai.probes.chrome_a11y import (  # noqa: PLC0415
        CHROME_DEFAULT,
        launch_chrome,
        stop_chrome,
    )
    from steerai.probes.layer_b import _ours  # noqa: PLC0415
    from steerai.probes.layer_b_pages import PageServer  # noqa: PLC0415
    from steerai.probes.uia_snapshot import UiaClient  # noqa: PLC0415
    from steerai.probes.win_input import ensure_per_monitor_v2  # noqa: PLC0415
    from steerai.probes.windows import wait_window_for_pid  # noqa: PLC0415
    from steerai.world.model import WorldModel  # noqa: PLC0415
    from steerai.world.observe2 import document_nodes  # noqa: PLC0415

    ensure_per_monitor_v2()  # before any rectangle is read
    models: list[WorldModel] = [WorldModel(UiaClient())] if client_first else []

    def read(hwnd: int) -> dict[str, Any]:
        if not models:
            models.append(WorldModel(UiaClient()))
        try:
            inside = document_nodes(models[0].observe(hwnd))
        except Exception as exc:  # noqa: BLE001 - a probe logs any COM failure as data
            return {"read_error": type(exc).__name__}
        return {"doc_nodes": None if inside is None else len(inside)}

    tmp = Path(tempfile.mkdtemp(prefix="steer-occlusion-"))
    row: dict[str, Any] = {"case": case, "run": number, "client_first": client_first}
    procs: list[tuple[Any, Path]] = []
    with PageServer(fixture.pages()) as server:
        port = fixture._free_port()
        extra = (NO_OCCLUSION,) if case == "covered_full_no_occlusion" else ()
        target = launch_chrome(
            CHROME_DEFAULT,
            tmp / "target",
            port,
            f"http://127.0.0.1:{server.port}/a",
            extra_args=extra,
        )
        procs.append((target, tmp / "target"))
        try:
            hwnd = wait_window_for_pid(target.pid, timeout_s=30.0, process=target).hwnd
            cdp, _ = _ours(port, server.port)
            try:
                fixture._wait_ready(cdp)
            finally:
                cdp.close()
            area = _work_area()
            half = (area[0] + area[2]) // 2
            if case == "background_visible":
                _place(hwnd, (area[0], area[1], half - 8, area[3]))
            if case == "covered_after_read":
                deadline = time.monotonic() + SPAN_S
                while (read(hwnd).get("doc_nodes") or 0) == 0:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("the page never showed before the cover")
                    time.sleep(SAMPLE_S)
                row["read_before_cover"] = True
            if case.startswith("covered") or case == "background_visible":
                cover = launch_chrome(
                    CHROME_DEFAULT, tmp / "cover", fixture._free_port(), "about:blank"
                )
                procs.append((cover, tmp / "cover"))
                cover_hwnd = wait_window_for_pid(cover.pid, timeout_s=30.0, process=cover).hwnd
                frame = _window_rect(hwnd)  # the whole window, its invisible border included
                rect = {
                    "covered_partial": (frame[0], frame[1], (frame[0] + frame[2]) // 2, frame[3]),
                    "background_visible": (half + 8, area[1], area[2], area[3]),
                }.get(case, frame)
                _place(cover_hwnd, rect)
            if case == "minimized":
                _minimise(hwnd)
            time.sleep(SETTLE_S)
            row["state"] = _state(hwnd)
            samples: list[dict[str, Any]] = []
            start = time.monotonic()
            while (t := time.monotonic() - start) < SPAN_S:
                samples.append({"t": round(t, 2), **read(hwnd)})
                time.sleep(max(0.0, SAMPLE_S - (time.monotonic() - start - t)))
            row["state_at_end"] = _state(hwnd)
            row["first_page_s"] = first_page(samples)
            row["last_doc_nodes"] = samples[-1].get("doc_nodes") if samples else None
            row["samples"] = len(samples)
        except (OSError, TimeoutError, RuntimeError) as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"[:200]
        finally:
            for proc, profile in reversed(procs):
                stop_chrome(proc, profile)
            shutil.rmtree(tmp, ignore_errors=True)
    return row


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - live
    parser = argparse.ArgumentParser(description="what a covered Chrome exposes to UIA")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--case", choices=CASES, action="append", help="default: every case")
    parser.add_argument(
        "--client-first",
        action="store_true",
        help="create the UIA client before Chrome starts (not the harness's order)",
    )
    parser.add_argument("--out", type=Path, required=True, help="one JSON line per run")
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        sys.stdout.write("occlusion: windows only\n")
        return 0
    with args.out.open("w", encoding="utf-8") as out:
        for case in args.case or CASES:
            for number in range(1, args.runs + 1):
                row = run_case(case, number, client_first=args.client_first)
                line = json.dumps(row)
                out.write(line + "\n")
                out.flush()
                sys.stdout.write(line + "\n")
                sys.stdout.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover - live
    raise SystemExit(main())
