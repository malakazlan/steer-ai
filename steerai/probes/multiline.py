"""[V2.19] item 3, [V2.15] amendment 3: does Chrome expose `multiline` on a text field?

    uv run python -m steerai.probes.multiline

A local page from memory holds an `<input type=text>`, an `<input type=search>`, a `<textarea>`,
a `contenteditable` div and a `role=textbox aria-multiline=true` div. For each, reports its
ControlType, AriaRole, raw AriaProperties string and whether that string carries `multiline`
(and its value). The page text is made up. If no multi-line element carries it, the query-field
rule's multiline test recovers nothing and is not worth keeping ([V2.15]).

Exit 75 when the pre-flight refuses.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from typing import Any

from steerai.probes.memory_guard import EXIT_LOW_MEMORY, FLOOR_MB, LowMemory, refusal_row

PAGE = """<!doctype html><meta charset="utf-8"><title>Multiline</title>
<input type="text" id="f-text" aria-label="Single text">
<input type="search" id="f-search" aria-label="Single search">
<textarea id="f-area" aria-label="Many lines"></textarea>
<div id="f-ce" contenteditable="true" aria-label="Editable block"></div>
<div id="f-role" role="textbox" aria-multiline="true" tabindex="0" aria-label="Role block"></div>
<p id="ready">Multiline ready</p>
"""
IDS = ("f-text", "f-search", "f-area", "f-ce", "f-role")
_CONTROL_TYPES = {
    50004: "Edit",
    50003: "ComboBox",
    50026: "Group",
    50020: "Text",
    50030: "Document",
}


def parse_aria_properties(raw: str) -> dict[str, str]:
    """The `name=value;name=value` pairs of an AriaProperties string; a backslash escapes the next
    character (UIA_AriaPropertiesPropertyId, Microsoft Learn). A pair without `=` is dropped."""
    out: dict[str, str] = {}
    name: list[str] = []
    value: list[str] = []
    in_value = False
    escaped = False

    def end_pair() -> None:
        if in_value and name:
            out["".join(name)] = "".join(value)
        name.clear()
        value.clear()

    for char in raw:
        if escaped:
            (value if in_value else name).append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ";":
            end_pair()
            in_value = False
        elif char == "=" and not in_value:
            in_value = True
        else:
            (value if in_value else name).append(char)
    end_pair()
    return out


def element_row(
    element_id: str, control_type: int, role: str, raw: str
) -> dict[str, Any]:  # Any: JSON
    """One element's report row."""
    parsed = parse_aria_properties(raw)
    return {
        "id": element_id,
        "control_type": _CONTROL_TYPES.get(control_type, str(control_type)),
        "role": role,
        "aria_properties": raw,
        "multiline": parsed.get("multiline"),
        "multiline_exposed": "multiline" in parsed,
    }


def summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:  # Any: JSON
    """Whether any element exposes `multiline`, and which (by id)."""
    exposed = [str(r["id"]) for r in rows if r.get("multiline_exposed")]
    return {"report": "multiline", "elements": len(rows), "exposed_on": exposed}


def rows_live(floor_mb: int) -> list[dict[str, Any]]:  # pragma: no cover - live  # Any: JSON
    from steerai.probes.layer_b_pages import PageServer  # noqa: PLC0415
    from steerai.probes.live_browser import live_page  # noqa: PLC0415 - Windows only
    from steerai.probes.uia_props import _IDS, _find  # noqa: PLC0415 - Windows only

    rows: list[dict[str, Any]] = []
    with (
        PageServer({"/multiline": PAGE}) as server,
        live_page(f"http://127.0.0.1:{server.port}/multiline", floor_mb=floor_mb) as page,
    ):
        for element_id in IDS:
            element = _find(page.uia, page.mod, page.hwnd, element_id)
            if not element:
                rows.append({"id": element_id, "found": False})
                continue
            rows.append(
                element_row(
                    element_id,
                    int(element.CurrentControlType),
                    str(element.GetCurrentPropertyValue(_IDS["AriaRole"])),
                    str(element.GetCurrentPropertyValue(_IDS["AriaProperties"])),
                )
            )
    return rows


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - live
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--min-free-mb", type=int, default=FLOOR_MB)
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        sys.stdout.write(json.dumps({"skipped": "windows only"}) + "\n")
        return 0
    try:
        rows = rows_live(args.min_free_mb)
    except LowMemory as refused:
        sys.stdout.write(json.dumps(refusal_row(refused, [])) + "\n")
        sys.stderr.write(f"{refused}\n")
        return EXIT_LOW_MEMORY
    for row in (*rows, summary(rows)):
        sys.stdout.write(json.dumps(row) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
