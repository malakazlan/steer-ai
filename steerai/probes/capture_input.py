"""Phase 0 probe: capture and input facts on this machine.

  1. DPI: do UIA bounding rectangles and Win32 window rectangles agree (Per-Monitor-V2 aware)?
  2. Foreground: which procedure lets this process bring another app's window to the foreground?
  3. Capture: per-window BitBlt vs PrintWindow latency; does GPU-rendered Chrome capture blank?

Usage (Windows):
    uv run python -m steerai.probes.capture_input [--out r.json]
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import json
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from ctypes import wintypes
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from steerai.probes.chrome_a11y import CHROME_DEFAULT, build_test_page, launch_chrome
from steerai.probes.stats import LatencyStats
from steerai.probes.uia_snapshot import UiaClient
from steerai.probes.windows import wait_window_for_pid

Rect = tuple[int, int, int, int]
CaptureMethod = Literal["bitblt", "printwindow"]
_PW_RENDERFULLCONTENT = 0x0000_0002
_SRCCOPY = 0x00CC_0020
_DIB_RGB_COLORS = 0
_BI_RGB = 0


# ---------------------------------------------------------------- pure helpers


def rects_agree(a: Rect, b: Rect, *, tolerance: int) -> bool:
    return all(abs(x - y) <= tolerance for x, y in zip(a, b, strict=True))


def image_is_blank(pixels: bytes, *, stride_px: int = 1) -> bool:
    """True when every sampled 32-bit pixel is identical (black, white, or any flat colour)."""
    if not pixels:
        raise ValueError("no pixels")
    step = 4 * max(stride_px, 1)
    first = pixels[0:4]
    return all(pixels[i : i + 4] == first for i in range(0, len(pixels) - 3, step))


# ---------------------------------------------------------------- Windows


def uia_rect_of_hwnd(hwnd: int) -> Rect:
    return UiaClient().bounding_rect(hwnd)


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = (
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    )


class _BITMAPINFO(ctypes.Structure):
    _fields_ = (("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3))


@dataclass(frozen=True, slots=True)
class Capture:
    method: str
    width: int
    height: int
    elapsed_ms: float
    blank: bool


def capture_window(hwnd: int, *, method: CaptureMethod) -> Capture:
    """Capture one window into a 32-bpp DIB and report size, latency and blankness."""
    if sys.platform != "win32":
        raise OSError("capture requires Windows")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise OSError(f"GetWindowRect failed: {ctypes.get_last_error()}")
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width <= 0 or height <= 0:
        raise OSError(f"window {hwnd:#x} has empty rect")

    info = _BITMAPINFO()
    info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    info.bmiHeader.biWidth = width
    info.bmiHeader.biHeight = -height  # top-down
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 32
    info.bmiHeader.biCompression = _BI_RGB

    screen_dc = user32.GetDC(None)
    mem_dc = gdi32.CreateCompatibleDC(screen_dc)
    bits = ctypes.c_void_p()
    gdi32.CreateDIBSection.restype = wintypes.HBITMAP
    bitmap = gdi32.CreateDIBSection(
        screen_dc, ctypes.byref(info), _DIB_RGB_COLORS, ctypes.byref(bits), None, 0
    )
    if not bitmap:
        raise OSError(f"CreateDIBSection failed: {ctypes.get_last_error()}")
    previous = gdi32.SelectObject(mem_dc, bitmap)
    try:
        started = time.perf_counter_ns()
        if method == "bitblt":
            ok = gdi32.BitBlt(mem_dc, 0, 0, width, height, screen_dc, rect.left, rect.top, _SRCCOPY)
        else:
            ok = user32.PrintWindow(hwnd, mem_dc, _PW_RENDERFULLCONTENT)
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
        if not ok:
            raise OSError(f"{method} failed: {ctypes.get_last_error()}")
        size = width * height * 4
        pixels = ctypes.string_at(bits, size)
    finally:
        gdi32.SelectObject(mem_dc, previous)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(None, screen_dc)
    return Capture(
        method=method,
        width=width,
        height=height,
        elapsed_ms=elapsed_ms,
        blank=image_is_blank(pixels, stride_px=97),
    )


# ---------------------------------------------------------------- probe


@dataclass(frozen=True, slots=True)
class ProbeArgs:
    chrome: Path
    runs: int
    out: str | None


def parse_args(argv: Sequence[str]) -> ProbeArgs:
    parser = argparse.ArgumentParser(description="Capture and input facts on this machine.")
    parser.add_argument("--chrome", type=Path, default=CHROME_DEFAULT)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--out", default=None)
    ns = parser.parse_args(argv)
    return ProbeArgs(chrome=ns.chrome, runs=ns.runs, out=ns.out)


def _capture_stats(hwnd: int, method: CaptureMethod, runs: int) -> dict[str, Any]:
    captures = [capture_window(hwnd, method=method) for _ in range(runs)]
    stats = LatencyStats.from_samples([c.elapsed_ms for c in captures])
    return {
        "method": method,
        "width": captures[-1].width,
        "height": captures[-1].height,
        "blank_runs": sum(c.blank for c in captures),
        "runs": runs,
        **{k: v for k, v in asdict(stats).items() if k != "n"},
    }


def run_probe(args: ProbeArgs) -> dict[str, Any]:
    # Lazy import: Windows-only.
    from steerai.probes import win_input  # noqa: PLC0415

    result: dict[str, Any] = {"dpi_system": win_input.system_dpi()}
    notepad = subprocess.Popen(["notepad.exe"])  # noqa: S607 - fixed, well-known executable
    profile_dir = Path(tempfile.mkdtemp(prefix="steer-cap-"))
    chrome: subprocess.Popen[bytes] | None = None
    previous_foreground = win_input.foreground_hwnd()
    try:
        pad = wait_window_for_pid(notepad.pid)
        result["notepad"] = asdict(pad)

        uia_rect = uia_rect_of_hwnd(pad.hwnd)
        win_rect = win_input.window_rect(pad.hwnd)
        result["dpi"] = {
            "uia_rect": uia_rect,
            "win32_rect": win_rect,
            "agree": rects_agree(uia_rect, win_rect, tolerance=1),
        }

        result["foreground"] = {
            "acquire_notepad": win_input.acquire_foreground(pad.hwnd),
            "restore_previous": win_input.acquire_foreground(previous_foreground)
            if previous_foreground
            else "none",
        }

        page = profile_dir / "probe.html"
        page.write_text(build_test_page(links=50, buttons=10), encoding="utf-8")
        chrome = launch_chrome(args.chrome, profile_dir, 9334, page.as_uri())
        chrome_window = wait_window_for_pid(chrome.pid, title_contains="Google Chrome")
        result["chrome"] = asdict(chrome_window)

        result["capture"] = {
            "notepad": [_capture_stats(pad.hwnd, m, args.runs) for m in ("bitblt", "printwindow")],
            "chrome": [
                _capture_stats(chrome_window.hwnd, m, args.runs) for m in ("bitblt", "printwindow")
            ],
        }
    finally:
        for proc in (notepad, chrome):
            if proc is None:
                continue
            proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=10)
        shutil.rmtree(profile_dir, ignore_errors=True)
    return result


def format_report(result: dict[str, Any]) -> str:
    lines = [f"system dpi {result['dpi_system']}"]
    dpi = result["dpi"]
    lines.append(f"dpi agree: {dpi['agree']}  uia={dpi['uia_rect']} win32={dpi['win32_rect']}")
    fg = result["foreground"]
    lines.append(
        f"foreground: notepad via {fg['acquire_notepad']}, restore via {fg['restore_previous']}"
    )
    for app, rows in result["capture"].items():
        for row in rows:
            lines.append(
                f"capture {app:<8} {row['method']:<12} {row['width']}x{row['height']} "
                f"p50 {row['p50_ms']:.1f} ms  p95 {row['p95_ms']:.1f} ms  "
                f"blank {row['blank_runs']}/{row['runs']}"
            )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    result = run_probe(args)
    sys.stdout.write(format_report(result) + "\n")
    if args.out is not None:
        Path(args.out).write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
