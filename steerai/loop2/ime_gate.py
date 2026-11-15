"""[V2.63] Q3: the open-IME probe is waived for stage 2, but the unprobed case fails closed.

At start, v2 reads the installed input methods. When a Chinese, Japanese or Korean IME is among
them and the open-IME probe (`probes.ime_probe`) has no recorded pass on this machine covering
every such IME, with the probe key the monitor sends, v2 refuses with `ime_unprobed`. On an
en-US-only machine the record is never read. The record lives beside the price file
(`%LOCALAPPDATA%/steer/ime_probe.json`); the probe writes it only on a pass whose restore read
back as before.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from steerai.probes.ime_probe import RECORD_NAME, ime_layouts, parse_hkl
from steerai.session.monitor import PROBE_VK

UNPROBED: Final = "ime_unprobed"
Record = Mapping[str, Any]  # Any: JSON values


def steer_dir() -> Path | None:
    """`%LOCALAPPDATA%/steer`, where the price file lives; None with LOCALAPPDATA unset."""
    root = os.environ.get("LOCALAPPDATA")
    return None if not root else Path(root) / "steer"


def read_record(folder: Path | None) -> dict[str, Any] | None:  # Any: JSON values
    """The probe's record in `folder`; None when there is none. Unreadable or not an object:
    OSError or ValueError, which `ime_refusal` treats as no pass."""
    if folder is None:
        return None
    path = folder / RECORD_NAME
    if not path.exists():
        return None
    record = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(record, dict):
        raise ValueError("the IME probe record is not an object")
    return record


def ime_refusal(installed: Sequence[int], record: Callable[[], Record | None]) -> str | None:
    """`ime_unprobed` when an installed CJK IME is not covered by a recorded pass with the
    monitor's probe key; None otherwise. Anything unread or odd in the record is no pass."""
    needed = set(ime_layouts(installed))
    if not needed:
        return None
    try:
        found = record()
    except (OSError, ValueError):
        return UNPROBED
    if (
        not isinstance(found, Mapping)
        or found.get("passed") is not True
        or found.get("probe_vk") != f"{PROBE_VK:#04x}"
    ):
        return UNPROBED
    layouts = found.get("layouts")
    if not isinstance(layouts, list) or not all(isinstance(x, str) for x in layouts):
        return UNPROBED
    try:
        covered = {parse_hkl(x) for x in layouts}
    except ValueError:
        return UNPROBED
    return None if needed <= covered else UNPROBED
