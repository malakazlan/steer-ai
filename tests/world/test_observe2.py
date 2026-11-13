"""The v2 observe path (step 3b, [V2.5]): v1's snapshot plus separate reads; a failed read is
None (not observed), the value is kept only as a `ValueFact`, and a secure field is never read."""

from __future__ import annotations

from steerai.world.address_bar import DocumentUrl
from steerai.world.effects import value_fact
from steerai.world.observe2 import read_observed
from tests.world.test_effects_conditions import BASE, _snap

KEY = b"k" * 32


class Reader:
    def __init__(self, *, fail: frozenset[str] = frozenset()) -> None:
        self.fail = fail
        self.calls: list[str] = []
        self.hwnds: list[int] = []

    def document_url(self, hwnd: int) -> DocumentUrl | None:
        self.calls.append("url")
        self.hwnds.append(hwnd)
        if "url" in self.fail:
            raise OSError("gone")
        return DocumentUrl(runtime_id=(9, 1), url="https://shop.test/?q=red")

    def focused_runtime_id(self) -> tuple[int, ...]:
        self.calls.append("focus")
        if "focus" in self.fail:
            raise OSError("gone")
        return (9, 2)

    def roles_and_headings(
        self, hwnd: int
    ) -> tuple[dict[tuple[int, ...], str], dict[tuple[int, ...], int]]:
        self.calls.append("roles")
        self.hwnds.append(hwnd)
        if "roles" in self.fail:
            raise OSError("gone")
        return {(9, 2): "searchbox"}, {(9, 4): 1}


def test_every_fact_is_read_and_the_value_is_only_a_fact() -> None:
    seen = read_observed(
        _snap(BASE), Reader(), key=KEY, windows=lambda: (16,), acted_value=lambda: "red shoes"
    )
    assert seen.url == "https://shop.test/?q=red"
    assert seen.focus == (9, 2)
    assert seen.windows == (16,)
    assert seen.aria_roles == {(9, 2): "searchbox"}
    assert seen.headings == {(9, 4): 1}
    assert seen.acted_value == value_fact("red shoes", key=KEY)
    assert "red shoes" not in repr(seen)


def test_a_failed_read_is_none_never_a_guess() -> None:
    seen = read_observed(
        _snap(BASE),
        Reader(fail=frozenset({"url", "focus", "roles"})),
        key=KEY,
        windows=lambda: (16,),
    )
    assert (seen.url, seen.focus, seen.aria_roles, seen.headings) == (None, None, None, None)


def test_no_acted_reader_means_no_value_read() -> None:
    seen = read_observed(_snap(BASE), Reader(), key=KEY)
    assert seen.acted_value is None
    assert seen.windows is None


def test_a_window_without_a_page_skips_the_page_reads() -> None:
    reader = Reader()
    seen = read_observed(_snap(BASE), reader, key=KEY, page_facts=False)
    assert reader.calls == ["focus"]
    assert seen.url is None
    assert seen.headings is None
