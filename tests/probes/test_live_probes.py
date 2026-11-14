"""[V2.19] item 3: the pure parts of the landmarks, multiline, scrolled, controller_for probes."""

from __future__ import annotations

import pytest

from steerai.probes import controller_for, landmark_sites, multiline, scrolled
from steerai.probes.live_checks import CHECKS
from steerai.world.landmarks import Landmark, PageFacts

SEARCH = (9, 1)
OTHER = (9, 2)


def _facts() -> PageFacts:
    return PageFacts(
        roles={(1,): "searchbox", (2,): "textbox"},
        headings={},
        landmarks={
            (1,): Landmark("search", SEARCH),
            (2,): None,
            (3,): Landmark("search", SEARCH),
            (4,): Landmark("search", OTHER),
            (5,): Landmark("navigation", (9, 3)),
        },
    )


def test_a_field_reports_its_landmark_and_whether_a_button_shares_it() -> None:
    elements = [
        ((1,), 50004, False),
        ((2,), 50003, False),
        ((3,), 50000, False),
        ((6,), 50004, False),
        ((7,), 50004, True),
    ]
    rows = landmark_sites.landmark_rows(elements, _facts())
    assert rows == [
        {
            "control_type": "Edit",
            "role": "searchbox",
            "landmark": "search",
            "search_button_shared": True,
        },
        {
            "control_type": "ComboBox",
            "role": "textbox",
            "landmark": "none",
            "search_button_shared": False,
        },
        {
            "control_type": "Edit",
            "role": "",
            "landmark": "unobserved",
            "search_button_shared": False,
        },
    ]


def test_a_button_in_another_search_landmark_does_not_share() -> None:
    rows = landmark_sites.landmark_rows([((1,), 50004, False), ((4,), 50000, False)], _facts())
    assert rows[0]["search_button_shared"] is False


def test_a_non_search_landmark_never_counts_as_a_shared_button() -> None:
    facts = PageFacts(
        roles={},
        headings={},
        landmarks={(1,): Landmark("navigation", (9, 3)), (5,): Landmark("navigation", (9, 3))},
    )
    rows = landmark_sites.landmark_rows([((1,), 50004, False), ((5,), 50000, False)], facts)
    assert rows[0]["landmark"] == "navigation"
    assert rows[0]["search_button_shared"] is False


def test_aria_properties_parse_pairs_and_escapes() -> None:
    parsed = multiline.parse_aria_properties(r"multiline=true;haspopup=a\;b;required=false;junk")
    assert parsed == {"multiline": "true", "haspopup": "a;b", "required": "false"}
    assert multiline.parse_aria_properties("") == {}


def test_an_element_row_says_whether_multiline_is_exposed() -> None:
    row = multiline.element_row("f-area", 50004, "textbox", "multiline=true;required=false")
    assert row == {
        "id": "f-area",
        "control_type": "Edit",
        "role": "textbox",
        "aria_properties": "multiline=true;required=false",
        "multiline": "true",
        "multiline_exposed": True,
    }
    plain = multiline.element_row("f-text", 50004, "textbox", "required=false")
    assert plain["multiline_exposed"] is False
    assert plain["multiline"] is None


def test_the_multiline_summary_names_the_exposing_elements() -> None:
    rows = [
        multiline.element_row("a", 50004, "textbox", "multiline=true"),
        multiline.element_row("b", 50004, "textbox", ""),
    ]
    assert multiline.summary(rows) == {"report": "multiline", "elements": 2, "exposed_on": ["a"]}


def test_flips_count_only_elements_in_both_reads() -> None:
    before: dict[tuple[int, ...], bool] = {(1,): False, (2,): True, (3,): False}
    after: dict[tuple[int, ...], bool] = {(1,): True, (2,): True, (4,): True}
    assert scrolled.flipped(before, after) == 1


def test_the_page_down_run_stops_at_the_bottom() -> None:
    presses = [scrolled.Press(20.0, 3), scrolled.Press(99.7, 2)]
    assert scrolled.stop_reason(presses) == "bottom"


def test_the_run_stops_after_two_presses_that_move_nothing() -> None:
    assert scrolled.stop_reason([scrolled.Press(50.0, 0)]) is None
    assert scrolled.stop_reason([scrolled.Press(50.0, 0), scrolled.Press(50.0, 0)]) == "no_movement"
    assert scrolled.stop_reason([scrolled.Press(None, 0), scrolled.Press(None, 0)]) == "no_movement"


def test_an_unreadable_percent_with_flips_keeps_pressing_until_the_cap() -> None:
    presses = [scrolled.Press(None, 4) for _ in range(5)]
    assert scrolled.stop_reason(presses) is None
    assert scrolled.stop_reason(presses, cap=5) == "cap"


def test_a_press_row_compares_with_the_reading_before() -> None:
    row = scrolled.press_row(2, 10.0, scrolled.Press(30.0, 5), elements=120)
    assert row == {
        "press": 2,
        "percent": 30.0,
        "percent_readable": True,
        "percent_changed": True,
        "flipped": 5,
        "elements": 120,
    }
    unread = scrolled.press_row(1, None, scrolled.Press(None, 0), elements=1)
    assert unread["percent_readable"] is False
    assert unread["percent_changed"] is False


def test_the_scrolled_summary() -> None:
    rows = [
        scrolled.press_row(1, 0.0, scrolled.Press(10.0, 2), elements=5),
        scrolled.press_row(2, 10.0, scrolled.Press(10.0, 0), elements=5),
    ]
    out = scrolled.summary(rows, "no_movement")
    assert out["percent_ever_changed"] is True
    assert out["flips_ever"] is True
    assert out["stopped"] == "no_movement"


def test_the_controller_report_names_the_listbox_and_the_cost() -> None:
    rows = controller_for.report(
        [(1, 2), (3, 4)],
        (3, 4),
        standalone_ms=[1.0, 2.0, 3.0, 4.0],
        base_ms=[10.0, 11.0, 12.0, 13.0],
        with_ms=[11.0, 12.0, 14.0, 15.0],
    )
    assert rows[0] == {
        "check": "controller_for_names_listbox",
        "controlled": 2,
        "names_listbox": True,
    }
    assert rows[1] == {"timing": "standalone", "n": 4, "p50_ms": 2.0, "p95_ms": 4.0}
    assert rows[-1] == {
        "report": "controller_for",
        "fetch_extra_p50_ms": 1.0,
        "fetch_extra_p95_ms": 2.0,
    }


def test_an_empty_controller_for_does_not_name_the_listbox() -> None:
    assert controller_for.names_listbox([], (3, 4)) is False


def test_the_property_id_is_the_documented_one() -> None:
    assert controller_for.CONTROLLER_FOR == 30104


def test_every_check_is_now_built() -> None:
    assert [c.name for c in CHECKS if c.args is None] == []


@pytest.mark.parametrize("name", ["landmarks", "multiline", "scrolled", "controller_for"])
def test_each_new_check_runs_its_module_and_names_the_folder_free(name: str) -> None:
    (check,) = [c for c in CHECKS if c.name == name]
    assert check.args is not None
    assert check.args[0].startswith("steerai.probes.")
