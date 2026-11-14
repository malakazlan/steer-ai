"""[V2.14] decision 2: the data-only query-class analysis. Nothing here feeds the classifier.

35% of the pooled search tasks fall to commit under §7.2 (no searchbox role, no search landmark).
This scores one candidate rule against two sets:
- positives: the visible fields on those commit-class search sites' start pages;
- negatives: every visible field on the local gallery and pooled pages (never the sealed H7
  pages), none of which is a search field: composers, comment boxes, login, sign-up.

The candidate (all must hold; every §7.2 condition still applies on top): the field is
single-line; its name, automation id or group name holds a search word; no commit word (the
gate's own deny and control words) sits on the field or on a button in its group. The report
gives how many positives it recovers and which negatives it wrongly accepts (that must be 0).

Live collection is read-only (no input) and launches one throwaway Chrome at a time. The
multi-line fact is unmeasured: it is read from AriaProperties (`multiline=true`) and kept raw so
the probe shows whether Chrome exposes it; unread fails closed.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from steerai.orchestrator.gate import CONTROL_PATTERNS, DENY_PATTERNS
from steerai.verify.deny_skeleton import camel_split, holds

Source = Literal["positive", "negative"]
SEARCH_WORDS = ("search", "find", "query", "lookup", "look up")
"""The candidate's search words: a probe input under evaluation, not a rule."""
COMMIT_WORDS = tuple(sorted({*DENY_PATTERNS, *CONTROL_PATTERNS}))
FIELD_TYPES = (50004, 50003)
"""Edit and ComboBox."""
BUTTON_TYPE = 50000
GROUP_TYPE = 50026
DOCUMENT_TYPE = 50030
MAX_DEPTH = 40


@dataclass(frozen=True, slots=True)
class FieldFacts:
    source: Source
    task: str
    aria_role: str
    name: str
    automation_id: str
    multiline: bool | None
    """None: not read (fails closed)."""
    group_name: str
    """The nearest landmark or Group ancestor's name, else the Document's."""
    buttons: tuple[str, ...]
    """The names of the Buttons inside that group."""


def _has(text: str, words: Iterable[str]) -> bool:
    return bool(text) and any(holds(text, word) for word in words)


def candidate(f: FieldFacts) -> tuple[bool, str]:
    """The candidate rule and the first reason it fails, as a closed token."""
    if f.multiline is None:
        return False, "multiline_unread"
    if f.multiline:
        return False, "multiline"
    texts = (f.name, camel_split(f.automation_id), f.group_name)
    if not any(_has(t, SEARCH_WORDS) for t in texts):
        return False, "no_search_word"
    if _has(f.name, COMMIT_WORDS) or _has(camel_split(f.automation_id), COMMIT_WORDS):
        return False, "commit_word"
    if any(_has(b, COMMIT_WORDS) for b in f.buttons):
        return False, "commit_word"
    return True, "search_word"


def evaluate(rows: Sequence[FieldFacts]) -> dict[str, Any]:  # Any: JSON values
    """Positives recovered (by task); negatives wrongly accepted, by task and with the field's
    role and name, so a real search field on a local page is told apart by hand; and why each
    unrecovered positive failed."""
    positives = [r for r in rows if r.source == "positive"]
    negatives = [r for r in rows if r.source == "negative"]
    return {
        "positives": len(positives),
        "recovered": sorted({r.task for r in positives if candidate(r)[0]}),
        "negatives": len(negatives),
        "false_accepts": sorted({r.task for r in negatives if candidate(r)[0]}),
        "false_accept_fields": sorted(
            (r.task, r.aria_role, r.name) for r in negatives if candidate(r)[0]
        ),
        "fail_reasons": sorted(candidate(r)[1] for r in positives if not candidate(r)[0]),
    }


def collect(url: str, *, task: str, source: Source) -> list[FieldFacts]:  # pragma: no cover
    """Read every visible field on `url` in one throwaway Chrome. Windows only, read-only."""
    from steerai.probes.chrome_a11y import (  # noqa: PLC0415
        CHROME_DEFAULT,
        launch_chrome,
        stop_chrome,
    )
    from steerai.probes.layer_b import _free_port  # noqa: PLC0415
    from steerai.probes.uia_props import _IDS, _uia  # noqa: PLC0415
    from steerai.probes.windows import wait_window_for_pid  # noqa: PLC0415

    tmp = Path(tempfile.mkdtemp(prefix="steer-qfields-"))
    proc = launch_chrome(CHROME_DEFAULT, tmp / "profile", _free_port(), url)
    try:
        hwnd = wait_window_for_pid(proc.pid, image="chrome.exe", process=proc).hwnd
        time.sleep(6.0)  # the page settles its first layout
        uia, mod = _uia()
        found = uia.ElementFromHandle(hwnd).FindAll(
            mod.TreeScope_Descendants, uia.CreateTrueCondition()
        )
        rows = []
        for i in range(found.Length):
            element = found.GetElement(i)
            if element.CurrentControlType not in FIELD_TYPES or element.CurrentIsOffscreen:
                continue
            props = str(element.GetCurrentPropertyValue(_IDS["AriaProperties"]) or "")
            group = _group(uia, element, _IDS["LandmarkType"])
            rows.append(
                FieldFacts(
                    source=source,
                    task=task,
                    aria_role=str(element.GetCurrentPropertyValue(_IDS["AriaRole"]) or ""),
                    name=str(element.CurrentName or ""),
                    automation_id=str(element.CurrentAutomationId or ""),
                    multiline=_multiline(props),
                    group_name="" if group is None else str(group.CurrentName or ""),
                    buttons=() if group is None else _buttons(uia, mod, group),
                )
            )
        return rows
    finally:
        stop_chrome(proc, tmp / "profile")
        shutil.rmtree(tmp, ignore_errors=True)


def _multiline(props: str) -> bool | None:
    """AriaProperties is `name=value;...`; Chrome's exposure of `multiline` is unmeasured."""
    pairs = dict(p.split("=", 1) for p in props.split(";") if "=" in p)
    value = pairs.get("multiline")
    return None if value is None else value == "true"


def _group(uia: Any, element: Any, landmark_id: int) -> Any:  # pragma: no cover - Any: COM
    node = element
    for _ in range(MAX_DEPTH):
        node = uia.ControlViewWalker.GetParentElement(node)
        if not node:
            return None
        if node.GetCurrentPropertyValue(landmark_id) or node.CurrentControlType in (
            GROUP_TYPE,
            DOCUMENT_TYPE,
        ):
            return node
    return None


def _buttons(uia: Any, mod: Any, group: Any) -> tuple[str, ...]:  # pragma: no cover - Any: COM
    found = group.FindAll(
        mod.TreeScope_Descendants,
        uia.CreatePropertyCondition(mod.UIA_ControlTypePropertyId, BUTTON_TYPE),
    )
    return tuple(str(found.GetElement(i).CurrentName or "") for i in range(found.Length))


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - live
    """`--collect`: read both sets, one Chrome at a time, write JSON lines; then score them."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        sys.stdout.write(json.dumps({"skipped": "windows only"}) + "\n")
        return 0
    from steerai.probes.layer_b_pages import PageServer  # noqa: PLC0415
    from steerai.probes.task_suite import (  # noqa: PLC0415
        H2,
        H3,
        H4,
        H5,
        H6,
        HELD_OUT,
        PAGE_SETS,
        TASKS,
        _pages_for,
    )
    from steerai.probes.uia_props import site_fields  # noqa: PLC0415

    rows: list[FieldFacts] = []
    seen: set[str] = set()
    for task in (*TASKS, *HELD_OUT, *H2, *H3, *H4, *H5, *H6):
        if task.start in seen or task.start == "h7":
            continue
        seen.add(task.start)
        if task.start.startswith("https://"):
            if site_fields(task.start)["query_class_possible"]:
                continue  # already a query field under §7.2: not a positive
            rows += collect(task.start, task=task.task_id, source="positive")
        elif task.start in PAGE_SETS:  # local pages only; "h7" (sealed) is skipped above
            with PageServer(_pages_for(task)) as server:
                url = f"http://127.0.0.1:{server.port}/page.html"
                rows += collect(url, task=task.task_id, source="negative")
    args.out.write_text("\n".join(json.dumps(asdict(r)) for r in rows) + "\n", encoding="utf-8")
    sys.stdout.write(json.dumps(evaluate(rows)) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
