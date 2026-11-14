"""Step 3d: the instant-search fixture page builder, its log parser and the pass checks (pure),
and one positive control per variant (probe: a real Chrome over DevTools, no OS input)."""

from __future__ import annotations

import ast
import json
from html.parser import HTMLParser
from pathlib import Path

import pytest

from steerai.probes import instant_search
from steerai.probes.effect_pages import WATCH_DEADLINE_S, WATCH_SETTLE_S
from steerai.probes.instant_search import (
    CATALOG,
    LOG_KEY,
    QUERY,
    SLOW_DELAY_MS,
    VARIANTS,
    Entry,
    PageSpec,
    Variant,
    pages,
    parse_log,
    passes,
    path_of,
    render,
    run_control,
    spec,
)
from steerai.probes.layer_b_pages import outside_links

SOURCE = Path(instant_search.__file__)
D_DELAY_MS = round(WATCH_SETTLE_S * 1000 * 3)
"""[V2.13].1 (d)'s delay, from the single source of the watch quiet window
(`effect_pages.WATCH_SETTLE_S`): three quiet windows, so the results land after the type's
quiet window has expired."""


class _Tags(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))


def _tags(html: str) -> list[tuple[str, dict[str, str | None]]]:
    parser = _Tags()
    parser.feed(html)
    return parser.tags


# ---- the page builder -------------------------------------------------------------------------


@pytest.mark.parametrize("variant", VARIANTS)
def test_every_variant_is_a_search_form_with_a_search_field(variant: Variant) -> None:
    tags = _tags(render(spec(variant)))
    assert ("form", {"id": "find", "role": "search", "action": "#"}) in tags
    fields = [a for t, a in tags if t == "input"]
    assert len(fields) == 1
    assert fields[0]["type"] == "search"


@pytest.mark.parametrize("variant", VARIANTS)
def test_only_c_button_has_a_search_button(variant: Variant) -> None:
    buttons = [a for t, a in _tags(render(spec(variant))) if t == "button"]
    assert len(buttons) == (1 if variant == "c-button" else 0)


@pytest.mark.parametrize("variant", VARIANTS)
def test_no_page_links_or_loads_anything_outside_its_origin(variant: Variant) -> None:
    html = render(spec(variant))
    assert outside_links(html) == []
    assert all(t != "script" or "src" not in a for t, a in _tags(html))


@pytest.mark.parametrize("variant", VARIANTS)
def test_every_value_goes_into_the_page_script_as_json(variant: Variant) -> None:
    html = render(spec(variant))
    assert "__" not in html.replace("__proto__", ""), "every placeholder is filled"
    assert json.dumps(LOG_KEY) in html
    assert json.dumps(QUERY) in html
    assert json.dumps(list(CATALOG)) in html


def test_only_d_has_a_delay_and_it_is_a_page_parameter() -> None:
    assert [spec(v).delay_ms for v in VARIANTS] == [0, 0, 0, 0, SLOW_DELAY_MS]
    assert "var INSTANT = true, URL_QUERY = true, DELAY = 2200" in render(PageSpec("d", 2200))


def test_d_delay_is_derived_from_the_watch_quiet_window() -> None:
    """[V2.13].1: (d) lands after the quiet window whatever `WATCH_SETTLE_S` is, and before the
    watch deadline."""
    quiet_ms = WATCH_SETTLE_S * 1000
    assert quiet_ms < D_DELAY_MS < WATCH_DEADLINE_S * 1000
    page = spec("d", slow_delay_ms=D_DELAY_MS)
    assert f"DELAY = {D_DELAY_MS}" in render(page)


@pytest.mark.parametrize(
    ("variant", "instant", "url_query"),
    [
        ("a", "true", "true"),
        ("b", "true", "false"),
        ("c-button", "false", "true"),
        ("c-nobutton", "false", "false"),
        ("d", "true", "true"),
    ],
)
def test_each_variant_sets_its_behaviour(variant: Variant, instant: str, url_query: str) -> None:
    assert f"var INSTANT = {instant}, URL_QUERY = {url_query}," in render(spec(variant))


def test_a_bad_variant_or_delay_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown variant"):
        render(PageSpec("e"))  # type: ignore[arg-type]  # the refusal under test
    with pytest.raises(ValueError, match="delay_ms"):
        render(PageSpec("d", -1))
    with pytest.raises(ValueError, match="delay_ms"):
        render(PageSpec("d", instant_search.MAX_DELAY_MS + 1))


def test_pages_serves_every_variant_by_its_path() -> None:
    served = pages()
    assert sorted(served) == sorted(path_of(v) for v in VARIANTS)
    assert served["/d"] == render(spec("d"))


# ---- the log parser ---------------------------------------------------------------------------


def _log(*entries: dict[str, object]) -> str:
    return json.dumps(list(entries))


def _entry(ev: str, **flags: bool) -> dict[str, object]:
    base: dict[str, object] = {"ev": ev, "match": False, "url_query": False, "trusted": True}
    base["late"] = False
    base.update(flags)
    return base


def test_parse_log_reads_closed_booleans() -> None:
    raw = _log(_entry("results", match=True, url_query=True), _entry("enter"))
    assert parse_log(raw) == (
        Entry("results", True, True, True, False),
        Entry("enter", False, False, True, False),
    )


def test_an_empty_log_is_no_entries() -> None:
    assert parse_log("") == ()
    assert parse_log("[]") == ()


@pytest.mark.parametrize(
    "raw",
    [
        json.dumps({"ev": "results"}),
        _log({"ev": "typed", "match": True, "url_query": True, "trusted": True, "late": True}),
        _log({"ev": "results", "match": "yes", "url_query": True, "trusted": True, "late": True}),
        _log({"ev": "results", "match": True, "url_query": True, "trusted": True}),
        json.dumps(["results"]),
    ],
    ids=["not_a_list", "unknown_event", "text_flag", "missing_flag", "not_an_object"],
)
def test_any_other_log_shape_is_an_error(raw: str) -> None:
    with pytest.raises(ValueError, match="effect"):
        parse_log(raw)


# ---- the pass checks --------------------------------------------------------------------------

HIT_URL = Entry("results", True, True, True, True)
HIT_NO_URL = Entry("results", True, False, True, True)
MISS = Entry("results", False, True, True, True)
ENTER = Entry("enter", False, False, True, False)
BUTTON = Entry("button", False, False, True, False)
EARLY = Entry("results", True, True, True, False)
SCRIPTED = Entry("results", True, True, False, True)

CASES: list[tuple[Variant, tuple[Entry, ...], bool]] = [
    ("a", (HIT_URL,), True),
    ("a", (HIT_URL, ENTER), False),
    ("a", (HIT_NO_URL,), False),
    ("a", (MISS,), False),
    ("b", (HIT_NO_URL,), True),
    ("b", (HIT_URL,), False),
    ("b", (), False),
    ("c-button", (ENTER, BUTTON, HIT_URL), True),
    ("c-button", (ENTER,), False),
    ("c-button", (BUTTON, HIT_NO_URL), False),
    ("c-nobutton", (ENTER,), True),
    ("c-nobutton", (ENTER, HIT_URL), False),
    ("c-nobutton", (), False),
    ("d", (ENTER, HIT_URL), True),
    ("d", (ENTER, EARLY), False),
    ("d", (ENTER, HIT_NO_URL), False),
    ("a", (SCRIPTED,), False),
]


@pytest.mark.parametrize(("variant", "entries", "expected"), CASES)
def test_the_pass_check(variant: Variant, entries: tuple[Entry, ...], expected: bool) -> None:
    assert passes(variant, entries) is expected


# ---- isolation: no code shared with the loop or the verifier, nothing from H7 -----------------


def test_the_fixture_shares_no_code_with_the_loop_the_verifier_or_h7() -> None:
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.append(node.module)
    banned = ("steerai.loop2", "steerai.verify", "steerai.world", "steerai.probes.h7_page")
    assert [m for m in modules if m.startswith(banned)] == []


# ---- the positive controls: one per variant (probe; run by hand when memory allows) -----------


@pytest.mark.probe
@pytest.mark.timeout(120)
@pytest.mark.parametrize("variant", VARIANTS)
def test_the_positive_control_reaches_the_variants_end_state(variant: Variant) -> None:
    entries = run_control(variant, slow_delay_ms=D_DELAY_MS)
    assert passes(variant, entries), entries
