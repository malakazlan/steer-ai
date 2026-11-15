"""Why a fresh 3d page reads blank to `--loop v2` (stage 1 shakedown, 2026-11-15: 4 of 5 runs
ended `cannot / page_not_ready` after about 10 s with 0 model calls, while the fixture's DevTools
check said the body was ready). Read-only diagnostic.

Per run: one 3d variant served from memory on 127.0.0.1, Chrome launched in a throwaway profile
exactly as the stage 1 harness does (`launch_chrome` with its defaults, the window bound with
`wait_window_for_pid(pid, timeout_s=30.0, process=proc)`, then `_ours` and `_wait_ready`). From the
moment the harness would start its child, the bound window is read every 250 ms for 15 s, by v1's
client (`UiaClient.cached_tree`) and by v2's world observe (`WorldModel.observe`, as LiveWorld's
poll read). Each sample logs the bound window's class, whether it is still a window, the pid's
visible titled windows (handle and class), and in each read the Document count and the top
Document's descendant count. Titles are the fixture's own page titles; no other page text.

No input of any kind; nothing is brought to the front. Only the Chrome this probe launched is
closed, by its PID (`stop_chrome`).

Run (Windows, session unlocked, machine idle):
    uv run python -m steerai.probes.first_ready --runs 5 --out <file.jsonl>
"""

from __future__ import annotations

import argparse
import ctypes
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

from steerai.probes.element_list import elements_from_nodes
from steerai.world.effects import top_document
from steerai.world.elements import Element
from steerai.world.observe2 import document_nodes

SAMPLE_S: Final = 0.25
SPAN_S: Final = 15.0


def doc_counts(elements: Sequence[Element]) -> tuple[int, int | None]:
    """(how many Documents, how many elements under the top Document; None with no single top
    Document), on any element list."""
    docs = sum(e.role == "Document" for e in elements)
    children: dict[int | None, list[int]] = {}
    for element in elements:
        children.setdefault(element.parent_id, []).append(element.id)
    by_id = {e.id: e for e in elements}
    tops = [e for e in elements if e.role == "Document" and not _under_document(e, by_id)]
    if len(tops) != 1:
        return docs, None
    found: set[int] = set()
    pending = list(children.get(tops[0].id, []))
    while pending:
        current = pending.pop()
        if current not in found:
            found.add(current)
            pending.extend(children.get(current, []))
    return docs, len(found)


def _under_document(element: Element, by_id: dict[int, Element]) -> bool:
    parent = by_id.get(element.parent_id) if element.parent_id is not None else None
    seen: set[int] = set()
    while parent is not None and parent.id not in seen:
        if parent.role == "Document":
            return True
        seen.add(parent.id)
        parent = by_id.get(parent.parent_id) if parent.parent_id is not None else None
    return False


def first_nonempty(samples: Sequence[dict[str, Any]], key: str) -> float | None:  # Any: JSON
    """The first sample time at which `key`'s top Document had at least one element under it."""
    for sample in samples:
        count = sample.get(key)
        if isinstance(count, int) and count > 0:
            return float(sample["t"])
    return None


def _class_name(hwnd: int) -> str:  # pragma: no cover - live, Windows only
    buffer = ctypes.create_unicode_buffer(256)
    ctypes.windll.user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value


def _foreground() -> int:  # pragma: no cover - live, Windows only
    return int(ctypes.windll.user32.GetForegroundWindow() or 0)


def _is_window(hwnd: int) -> bool:  # pragma: no cover - live, Windows only
    return bool(ctypes.windll.user32.IsWindow(hwnd))


def run_once(  # noqa: PLR0913, PLR0915 - one live run, its options keyword-only  # pragma: no cover
    variant: str, number: int, out: Any, *, keep_cdp: bool, reader: str, occlude: bool
) -> dict[str, Any]:
    from steerai.probes import instant_search as fixture  # noqa: PLC0415 - live only
    from steerai.probes.chrome_a11y import (  # noqa: PLC0415
        CHROME_DEFAULT,
        launch_chrome,
        stop_chrome,
    )
    from steerai.probes.layer_b import _ours  # noqa: PLC0415
    from steerai.probes.layer_b_pages import PageServer  # noqa: PLC0415
    from steerai.probes.windows import list_top_windows, wait_window_for_pid  # noqa: PLC0415

    def pid_windows(pid: int) -> list[list[Any]]:
        return [[w.hwnd, _class_name(w.hwnd)] for w in list_top_windows() if w.pid == pid]

    tmp = Path(tempfile.mkdtemp(prefix="steer-first-ready-"))
    row: dict[str, Any] = {"run": number, "variant": variant}
    with PageServer(fixture.pages()) as server:
        port = fixture._free_port()
        base = f"http://127.0.0.1:{server.port}"
        launched = time.monotonic()
        proc = launch_chrome(CHROME_DEFAULT, tmp / "profile", port, base + f"/{variant}")
        cdp = None
        cover = None
        try:
            window = wait_window_for_pid(proc.pid, timeout_s=30.0, process=proc)
            row["bound_s"] = round(time.monotonic() - launched, 2)
            row["bound_class"] = _class_name(window.hwnd)
            row["bound_title"] = window.title
            row["pid_windows_at_bind"] = pid_windows(proc.pid)
            cdp, pages = _ours(port, server.port)
            row["page_targets"] = pages
            row["cdp_open"] = keep_cdp
            try:
                fixture._wait_ready(cdp)
                row["ready_s"] = round(time.monotonic() - launched, 2)
            finally:
                if not keep_cdp:
                    cdp.close()
            row["foreground_at_ready"] = _foreground() == window.hwnd
            if occlude:
                # a second throwaway Chrome at the same place and size covers the first (no input)
                cover = launch_chrome(
                    CHROME_DEFAULT, tmp / "cover", fixture._free_port(), "about:blank"
                )
                wait_window_for_pid(cover.pid, timeout_s=30.0, process=cover)
            row["occluded"] = occlude
            row["reader"] = reader
            if reader == "inproc":
                samples = sample_window(window.hwnd)
            else:
                samples = _child_samples(window.hwnd, mta=reader == "child-mta")
            for sample in samples:
                out.write(json.dumps({"run": number, "variant": variant, **sample}) + "\n")
            out.flush()
            row["pid_windows_at_end"] = pid_windows(proc.pid)
            row["v1_first_nonempty_s"] = first_nonempty(samples, "v1_top")
            row["v2_first_nonempty_s"] = first_nonempty(samples, "v2_top")
            row["samples"] = len(samples)
            row["last"] = samples[-1] if samples else None
        except (OSError, TimeoutError, RuntimeError) as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"[:200]
        finally:
            if keep_cdp and cdp is not None:
                cdp.close()
            if cover is not None:
                stop_chrome(cover, tmp / "cover")
            stop_chrome(proc, tmp / "profile")
            shutil.rmtree(tmp, ignore_errors=True)
    return row


def sample_window(hwnd: int) -> list[dict[str, Any]]:  # pragma: no cover - live
    """Every `SAMPLE_S` for `SPAN_S`, one read by v1's client and one by v2's world observe, each
    with its own client created here (on the calling thread)."""
    from steerai.probes.uia_snapshot import UiaClient  # noqa: PLC0415 - live only
    from steerai.world.model import WorldModel  # noqa: PLC0415

    v1 = UiaClient()
    v2 = WorldModel(UiaClient())
    samples: list[dict[str, Any]] = []
    start = time.monotonic()
    while (t := time.monotonic() - start) < SPAN_S:
        samples.append({"t": round(t, 2), **_sample(v1, v2, hwnd)})
        time.sleep(max(0.0, SAMPLE_S - (time.monotonic() - start - t)))
    return samples


COINIT_MULTITHREADED: Final = 0


def _sample_on_mta(hwnd: int) -> list[dict[str, Any]]:  # pragma: no cover - live
    """`sample_window` on a new thread that enters the COM MTA before any UIA object exists on
    it, as the v2 CLI's loop thread does (`live_world.enter_mta`)."""
    found: list[list[dict[str, Any]]] = []
    # comtypes enters its importing thread's apartment at import: import it here, as the CLI's
    # main thread does, so the reader thread can enter the MTA itself first
    import comtypes.client  # noqa: F401, PLC0415

    def body() -> None:
        result = ctypes.windll.ole32.CoInitializeEx(None, COINIT_MULTITHREADED) & 0xFFFFFFFF
        if result not in (0, 1):
            raise OSError("not the MTA")
        try:
            found.append(sample_window(hwnd))
        finally:
            ctypes.windll.ole32.CoUninitialize()

    thread = threading.Thread(target=body, name="first-ready-mta")
    thread.start()
    thread.join()
    if not found:
        raise RuntimeError("the MTA reader failed")
    return found[0]


def _child_samples(hwnd: int, *, mta: bool) -> list[dict[str, Any]]:  # pragma: no cover - live
    """The samples taken by a fresh interpreter, started the moment the harness starts its child:
    this process never asks UIA anything about the window first."""
    argv = [sys.executable, "-m", "steerai.probes.first_ready", "--sample-hwnd", str(hwnd)]
    done = subprocess.run(  # noqa: S603 - a fixed argument list, no shell
        [*argv, *(["--mta"] if mta else [])],
        capture_output=True,
        text=True,
        timeout=SPAN_S + 60,
        check=False,
    )
    if done.returncode != 0:
        raise RuntimeError(f"child reader exit {done.returncode}: {done.stderr[-300:]}")
    return [json.loads(line) for line in done.stdout.splitlines() if line.strip()]


def _sample(v1: Any, v2: Any, hwnd: int) -> dict[str, Any]:  # pragma: no cover - live
    """One read of `hwnd` by each client (Any: the UiaClient and the WorldModel, COM-backed)."""
    sample: dict[str, Any] = {"is_window": _is_window(hwnd), "foreground": _foreground() == hwnd}
    try:
        elements = elements_from_nodes(v1.cached_tree(hwnd).nodes)
        sample["v1_nodes"] = len(elements)
        sample["v1_docs"], sample["v1_top"] = doc_counts(elements)
    except Exception as exc:  # noqa: BLE001 - a diagnostic logs any COM failure
        sample["v1_error"] = type(exc).__name__
    try:
        snapshot = v2.observe(hwnd)
        inside = document_nodes(snapshot)
        sample["v2_nodes"] = len(snapshot.elements)
        sample["v2_stable"] = snapshot.stable
        sample["v2_docs"] = sum(e.role == "Document" for e in snapshot.elements)
        sample["v2_top"] = None if inside is None else len(inside)
        sample["v2_has_top"] = top_document(snapshot) is not None
    except Exception as exc:  # noqa: BLE001 - a diagnostic logs any COM failure
        sample["v2_error"] = type(exc).__name__
    sample["title"] = _title(hwnd) if sample["is_window"] else None
    return sample


def _title(hwnd: int) -> str:  # pragma: no cover - live, Windows only
    from steerai.probes.windows import window_title  # noqa: PLC0415

    return window_title(hwnd)


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - live
    parser = argparse.ArgumentParser(description="first-read diagnostic for the 3d pages")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument(
        "--keep-cdp", action="store_true", help="keep the DevTools session open, as the harness"
    )
    parser.add_argument(
        "--reader",
        choices=("inproc", "child-main", "child-mta"),
        default="inproc",
        help="who reads: this process, or a fresh interpreter on its main thread or an MTA thread",
    )
    parser.add_argument(
        "--occlude", action="store_true", help="cover the window with a second own Chrome"
    )
    parser.add_argument("--out", type=Path, help="every sample, as JSON lines")
    parser.add_argument("--sample-hwnd", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--mta", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        sys.stdout.write("first_ready: windows only\n")
        return 0
    if args.sample_hwnd is not None:  # the child reader
        samples = _sample_on_mta(args.sample_hwnd) if args.mta else sample_window(args.sample_hwnd)
        sys.stdout.write("".join(json.dumps(s) + "\n" for s in samples))
        return 0
    if args.out is None:
        parser.error("--out is required")
    from steerai.probes.instant_search import VARIANTS  # noqa: PLC0415 - live only

    with args.out.open("w", encoding="utf-8") as out:
        for number in range(1, args.runs + 1):
            variant = VARIANTS[(number - 1) % len(VARIANTS)]
            row = run_once(
                variant,
                number,
                out,
                keep_cdp=args.keep_cdp,
                reader=args.reader,
                occlude=args.occlude,
            )
            sys.stdout.write(json.dumps(row) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover - live
    raise SystemExit(main())
