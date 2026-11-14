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
import sys
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
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


NEGATIVE_PAGES: dict[str, str] = {
    "sw1": (
        "<!doctype html><title>Tasks</title><main><form aria-label='Tasks'>"
        "<input type='text' aria-label='Search or add a task'>"
        "<ul><li>Buy milk</li><li>Call Sam</li></ul></form></main>"
    ),
    "sw2": (
        "<!doctype html><title>Notes</title><main><section aria-label='Notes'>"
        "<input type='text' aria-label='Search notes'>"
        "<button type='button'>Delete</button><ul><li>Draft</li><li>Ideas</li></ul>"
        "</section></main>"
    ),
    "sw3": (
        "<!doctype html><title>Thread</title><main><section aria-label='Thread'>"
        "<p>Anyone seen the build log?</p>"
        "<input type='text' aria-label='Reply (find in thread)'>"
        "<button type='button'>Go</button></section></main>"
    ),
}
"""[V2.28] item 4: local negatives with a search word in a field that is not a query: a
single-line field where Enter adds an item and no commit word sits nearby (the candidate is
expected to accept it, which refutes the rule); a search field beside a Delete button; a
single-line reply box whose name holds a search word. Generic markup, no site or app names."""


class _Fields(HTMLParser):
    """The `FieldFacts` a UIA read of a generic page would give: each text input's name, the
    enclosing section or form's label as its group, and the group's button names."""

    def __init__(self) -> None:
        super().__init__()
        self._group: list[tuple[str, list[str]]] = []
        self._button = False
        self.rows: list[tuple[str, str, list[str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag in ("section", "form"):
            self._group.append((a.get("aria-label") or "", []))
        elif tag == "input" and a.get("type") == "text" and self._group:
            self.rows.append((a.get("aria-label") or "", self._group[-1][0], self._group[-1][1]))
        elif tag == "button":
            self._button = True

    def handle_endtag(self, tag: str) -> None:
        if tag in ("section", "form") and self._group:
            self._group.pop()
        elif tag == "button":
            self._button = False

    def handle_data(self, data: str) -> None:
        if self._button and self._group:
            self._group[-1][1].append(data.strip())


def negative_facts(task: str) -> list[FieldFacts]:
    """The fields `NEGATIVE_PAGES[task]` holds, as the live read would score them."""
    parser = _Fields()
    parser.feed(NEGATIVE_PAGES[task])
    return [
        FieldFacts("negative", task, "textbox", name, "", False, group, tuple(buttons))
        for name, group, buttons in parser.rows
    ]


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
    """Read every visible field inside the page's top Document on `url`, in one throwaway Chrome
    (guarded launch). Windows only, read-only.

    Live run 1: the window-wide search read only Chrome's omnibox ("Address and search bar") on
    every page. The search is now scoped to the Document `live_page` waited for, and a page with
    no field in it fails loudly (`no_page_fields`) instead of scoring an empty list."""
    from steerai.probes.live_browser import live_page, retry_until  # noqa: PLC0415
    from steerai.probes.memory_guard import FLOOR_MB  # noqa: PLC0415
    from steerai.probes.uia_props import _IDS  # noqa: PLC0415

    with live_page(url, floor_mb=FLOOR_MB, settle_s=6.0) as page:
        uia, mod = page.uia, page.mod

        def fields() -> list[Any]:  # Any: COM elements
            found = page.document.FindAll(mod.TreeScope_Descendants, uia.CreateTrueCondition())
            return [
                e
                for e in (found.GetElement(i) for i in range(found.Length))
                if e.CurrentControlType in FIELD_TYPES and not e.CurrentIsOffscreen
            ]

        elements = retry_until(fields, what="no_page_fields")
        rows = []
        for element in elements:
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


def _collected(  # pragma: no cover - live
    url: str, task: str, source: Source, errors: list[dict[str, Any]]
) -> list[FieldFacts]:
    """`collect`, with a page that never became readable recorded as an error row."""
    try:
        return collect(url, task=task, source=source)
    except RuntimeError as unreadable:  # TreeNotReady: no Document, or no field in it
        errors.append({"task": task, "error": str(unreadable)})
        return []


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
    errors: list[dict[str, Any]] = []
    seen: set[str] = set()
    for task in (*TASKS, *HELD_OUT, *H2, *H3, *H4, *H5, *H6):
        if task.start in seen or task.start == "h7":
            continue
        seen.add(task.start)
        if task.start.startswith("https://"):
            if site_fields(task.start)["query_class_possible"]:
                continue  # already a query field under §7.2: not a positive
            rows += _collected(task.start, task.task_id, "positive", errors)
        elif task.start in PAGE_SETS:  # local pages only; "h7" (sealed) is skipped above
            with PageServer(_pages_for(task)) as server:
                url = f"http://127.0.0.1:{server.port}/page.html"
                rows += _collected(url, task.task_id, "negative", errors)
    for name, html in NEGATIVE_PAGES.items():
        with PageServer({"/page.html": html}) as server:
            url = f"http://127.0.0.1:{server.port}/page.html"
            rows += _collected(url, name, "negative", errors)
    args.out.write_text("\n".join(json.dumps(asdict(r)) for r in rows) + "\n", encoding="utf-8")
    for error in errors:
        sys.stdout.write(json.dumps(error) + "\n")
    sys.stdout.write(json.dumps(evaluate(rows)) + "\n")
    from steerai.probes.live_browser import exit_code  # noqa: PLC0415

    return exit_code(errors)


if __name__ == "__main__":
    raise SystemExit(main())
