"""The effect observer's rules ([V2.2] §6.1/§6.4; packet v2-step2-effect-observer v3;
principal items 1-8, hunter EO-01..43)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from steerai.world.effects import Observed, observe
from tests.world.test_effects_conditions import BASE, FIELD, ITEM, LIST, URL, Row, _snap

HOME = "https://shop.test/"
RESULTS = "https://shop.test/search?q=red+shoes"


def _obs(
    rows: Sequence[Row], *, url: str | None = HOME, title: str = "Shop", **kw: object
) -> Observed:
    snapshot = replace(_snap(rows, **kw), title=title)  # type: ignore[arg-type]  # test kwargs
    return Observed(snapshot=snapshot, url=url, windows=(16,))


# ---- navigation and the URL --------------------------------------------------------------------


def test_a_new_top_document_is_navigated_and_suppresses_the_rest() -> None:
    new_page = (11, "Document", "Results", (9, 101), None)
    after = _obs(
        [new_page, (12, "Text", "Red shoes", (9, 102), 11)], url=RESULTS, title="Red shoes"
    )
    report = observe(_obs(BASE), after, acted=FIELD[3], text="red shoes")
    assert report.classes == frozenset({"navigated"})
    assert {"url_param", "title"} <= report.carriers


def test_a_replaced_top_document_of_the_same_page_is_a_rerender() -> None:
    same_page = (11, "Document", "Shop", (9, 101), None)
    rows = [same_page, (12, "Edit", "Search", (9, 102), 11), (13, "List", "Products", (9, 103), 11)]
    report = observe(_obs(BASE), _obs([*rows, (14, "ListItem", "Blue shoes", (9, 104), 13)]))
    assert "navigated" not in report.classes
    assert "rerendered" in report.tokens
    assert "none" not in report.classes


def test_a_url_change_on_the_same_document_is_in_place() -> None:
    report = observe(_obs(BASE), _obs(BASE, url=RESULTS), acted=FIELD[3], text="red shoes")
    assert "url_changed_in_place" in report.classes
    assert "url_param" in report.carriers


def test_exactly_one_side_with_a_top_document_is_unknown() -> None:
    report = observe(_obs(BASE), _obs([(*FIELD[:4], None), LIST, ITEM]))
    assert report.classes == frozenset({"unknown"})


def test_a_native_window_without_documents_still_compares() -> None:
    pane = (1, "Pane", "Notepad", (9, 1), None)
    edit = (2, "Document", "", (9, 2), 1)
    rows = [pane, (2, "Edit", "Text editor", (9, 2), 1)]
    report = observe(_obs(rows, url=None), _obs(rows, url=None))
    assert "unknown" not in report.classes
    assert edit[1] == "Document"  # the rows above hold none


def test_a_different_window_is_unknown() -> None:
    other = replace(_obs(BASE).snapshot, hwnd=0x20)
    report = observe(_obs(BASE), Observed(snapshot=other, url=HOME, windows=(16,)))
    assert report.classes == frozenset({"unknown"})


# ---- the acted element ---------------------------------------------------------------------


def test_a_caps_only_change_on_the_acted_element_is_own_state() -> None:
    folder: Row = (5, "TreeItem", "Reports", (9, 5), 3)
    before = _obs([*BASE, folder], caps={5: ("collapsed",)})
    after = _obs([*BASE, folder], caps={5: ("expanded",)})
    report = observe(before, after, acted=folder[3])
    assert {"own_state", "revealed"} <= report.classes, "expanded is gained"


def test_children_appearing_under_the_acted_element_are_revealed_with_their_root() -> None:
    button: Row = (5, "Button", "Sort", (9, 5), 1)
    menu: Row = (6, "Menu", "Sort order", (9, 6), 5)
    entry: Row = (7, "MenuItem", "Newest first", (9, 7), 6)
    report = observe(_obs([*BASE, button]), _obs([*BASE, button, menu, entry]), acted=button[3])
    assert "revealed" in report.classes
    assert report.revealed_root == menu[0]
    assert "content_changed" not in report.classes


def test_a_reused_runtime_id_with_another_role_is_never_own_state() -> None:
    old: Row = (5, "Button", "Mute", (9, 5), 1)
    new: Row = (5, "Text", "Mute", (9, 5), 1)
    before = _obs([*BASE, old], caps={5: ("toggle:off",)})
    after = _obs([*BASE, new], caps={5: ("toggle:on",)})  # a caps-only change, if it were one
    report = observe(before, after, acted=old[3])
    assert "own_state" not in report.classes


def test_unread_caps_never_give_own_state() -> None:
    folder: Row = (5, "TreeItem", "Reports", (9, 5), 3)
    before = _obs([*BASE, folder], caps={5: ("collapsed",)}, caps_read=False)
    after = _obs([*BASE, folder], caps={5: ("expanded",)})
    report = observe(before, after, acted=folder[3])
    assert "own_state" not in report.classes
    assert "caps" in report.unread


# ---- carriers --------------------------------------------------------------------------------


def test_instant_results_carry_the_text_but_the_fields_own_suggestions_do_not() -> None:
    suggestions: Row = (6, "List", "Suggestions", (9, 6), 2)
    suggestion: Row = (7, "ListItem", "red shoes size 9", (9, 7), 6)
    report = observe(
        _obs(BASE), _obs([*BASE, suggestions, suggestion]), acted=FIELD[3], text="red shoes"
    )
    assert "results" not in report.carriers, "an echo under the field is no result (EO-01/02)"
    result: Row = (8, "ListItem", "Red shoes for running", (9, 8), 3)
    report = observe(_obs(BASE), _obs([*BASE, result]), acted=FIELD[3], text="red shoes")
    assert "results" in report.carriers
    assert "content_changed" in report.classes


def test_a_carrier_already_present_before_is_none() -> None:
    shown: Row = (8, "ListItem", "Red shoes for running", (9, 8), 3)
    report = observe(
        _obs([*BASE, shown], url=RESULTS), _obs([*BASE, shown], url=RESULTS), text="red shoes"
    )
    assert report.carriers == frozenset()


def test_short_or_empty_text_carries_nothing_on_the_page() -> None:
    result: Row = (8, "ListItem", "Go to the shop", (9, 8), 3)
    for text in ("go", "", "  ", None):
        report = observe(_obs(BASE), _obs([*BASE, result], title="Go"), text=text)
        assert not ({"title", "results"} & report.carriers), repr(text)


def test_a_path_segment_carries_the_text_whole_never_a_substring() -> None:
    page = "https://wiki.test/wiki/Serendipity"
    report = observe(_obs(BASE), _obs(BASE, url=page), text="serendipity")
    assert "url_path" in report.carriers
    report = observe(
        _obs(BASE), _obs(BASE, url="https://wiki.test/wiki/Serendipityx"), text="serendipity"
    )
    assert "url_path" not in report.carriers


def test_an_autocompleted_query_is_not_the_text() -> None:
    report = observe(
        _obs(BASE), _obs(BASE, url="https://shop.test/search?q=red+shoes+sale"), text="red shoes"
    )
    assert "url_param" not in report.carriers, "carries is exact parameter equality"


# ---- what is not an effect -------------------------------------------------------------------


def test_transient_roles_are_not_changes() -> None:
    spinner: Row = (9, "ProgressBar", "Loading", (9, 9), 1)
    report = observe(_obs(BASE), _obs([*BASE, spinner]), acted=FIELD[3])
    assert report.classes == frozenset({"none"})


def test_an_element_without_a_runtime_id_appearing_is_a_change() -> None:
    anonymous: Row = (9, "Text", "Saved", (), 1)
    report = observe(_obs(BASE), _obs([*BASE, anonymous]), acted=FIELD[3])
    assert "content_changed" in report.classes


def test_a_title_change_alone_is_a_token() -> None:
    report = observe(_obs(BASE), _obs(BASE, title="(3) Shop"), acted=FIELD[3])
    assert "title_only" in report.tokens
    assert "none" not in report.classes


def test_the_report_is_pure_and_round_trips_through_json() -> None:
    from steerai.world.model import snapshot_from_json, snapshot_to_json  # noqa: PLC0415

    result: Row = (8, "ListItem", "Red shoes", (9, 8), 3)
    before, after = _obs(BASE), _obs([*BASE, result], url=RESULTS)
    first = observe(before, after, acted=FIELD[3], text="red shoes")
    again = observe(
        replace(before, snapshot=snapshot_from_json(snapshot_to_json(before.snapshot))),
        replace(after, snapshot=snapshot_from_json(snapshot_to_json(after.snapshot))),
        acted=FIELD[3],
        text="red shoes",
    )
    assert first == again
    assert URL == HOME
