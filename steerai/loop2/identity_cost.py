"""LW-03's cost (#361 T1): how long one read of a window's identity takes (v2's
`WindowsPlatform.window_identity`, over v1's `window_identity`), on a real window.

It reads the shell's desktop window (Progman), or `--hwnd`, `--reads` times and prints one JSON
line: the reads and p50, p95 and max in milliseconds. Read-only: no input, no window touched.

Run: `uv run python -m steerai.loop2.identity_cost [--reads 2000] [--hwnd N]`.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Sequence

from steerai.loop2.live_world import WindowIdentity


def measure(read: Callable[[], WindowIdentity | None], reads: int) -> dict[str, float | int]:
    """`reads` timed calls of `read`; the identity must be readable every time."""
    if reads < 1:
        raise ValueError("reads must be at least 1")
    samples: list[float] = []
    for _ in range(reads):
        started = time.perf_counter()
        identity = read()
        samples.append((time.perf_counter() - started) * 1000)
        if identity is None:
            raise OSError("the window's identity could not be read")
    samples.sort()
    return {
        "reads": reads,
        "p50_ms": round(samples[(reads - 1) // 2], 3),
        "p95_ms": round(samples[min(reads - 1, int(0.95 * reads))], 3),
        "max_ms": round(samples[-1], 3),
    }


def main(argv: Sequence[str] | None = None) -> int:
    from steerai.loop2.live_world import WindowsPlatform  # noqa: PLC0415 - Windows only
    from steerai.probes.windows import desktop_window  # noqa: PLC0415 - Windows only

    parser = argparse.ArgumentParser(prog="python -m steerai.loop2.identity_cost")
    parser.add_argument("--reads", type=int, default=2000)
    parser.add_argument("--hwnd", type=lambda t: int(t, 0), default=None)
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    hwnd = args.hwnd if args.hwnd is not None else desktop_window().hwnd
    platform = WindowsPlatform()
    row = measure(lambda: platform.window_identity(hwnd), args.reads)
    sys.stdout.write(json.dumps({"probe": "identity_cost", **row}) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
