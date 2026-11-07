"""The RuntimeId stability probe's pure counting (#89): which elements kept their RuntimeId."""

from steerai.probes.runtime_id_probe import Kept, kept_ids
from steerai.world.elements import Element


def _el(role: str, name: str, rid: tuple[int, ...]) -> Element:
    return Element(id=0, depth=0, role=role, name=name, box=(0, 0, 1, 1), runtime_id=rid)


def test_counts_the_search_box_and_result_links_that_kept_their_runtime_id() -> None:
    first = [
        _el("ComboBox", "Search", (1, 1)),
        _el("Hyperlink", "Drake - new song", (1, 2)),
        _el("Hyperlink", "Drake live", (1, 3)),
        _el("Button", "Drake", (1, 4)),
    ]
    second = [
        _el("ComboBox", "Search", (1, 1)),
        _el("Hyperlink", "Drake - new song", (1, 9)),
        _el("Hyperlink", "Drake live", (1, 3)),
    ]
    assert kept_ids(first, second, word="drake") == Kept(box_same=1, box_n=1, link_same=1, link_n=2)


def test_a_link_gone_on_the_second_look_did_not_keep_its_runtime_id() -> None:
    first = [_el("Hyperlink", "Drake one", (2, 1))]
    assert kept_ids(first, [], word="drake") == Kept(box_same=0, box_n=0, link_same=0, link_n=1)


def test_at_most_ten_links_are_counted() -> None:
    first = [_el("Hyperlink", f"drake {n}", (3, n)) for n in range(12)]
    assert kept_ids(first, first, word="drake").link_n == 10
