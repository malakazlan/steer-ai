"""World model boundaries: ids without a RuntimeId, duplicates, caps of 0 and 1, same-snapshot
resolution, and `observe` driven by a fake client with an injected clock."""

from types import SimpleNamespace
from typing import cast

import pytest

from steerai.errors import ErrorCode, SteerError
from steerai.probes.uia_snapshot import RawNode, TreeSnapshot, UiaClient
from steerai.world import model as model_module
from steerai.world.elements import Element
from steerai.world.model import Snapshot, WorldModel, diff, resolve_in

BOX = (0, 0, 10, 10)
HWND = 0x10


def _el(
    idx: int, role: str, name: str, rid: tuple[int, ...] = (), *, offscreen: bool = False
) -> Element:
    return Element(
        id=idx, depth=0, role=role, name=name, box=BOX, runtime_id=rid, offscreen=offscreen
    )


def _snap(*elements: Element, snapshot_id: str = "s1", cap: int = 150) -> Snapshot:
    return Snapshot(
        id=snapshot_id,
        hwnd=HWND,
        process="test.exe",
        title="t",
        taken_at=0.0,
        elements=list(elements),
        node_count=len(elements),
        stable=True,
        handles=[None] * len(elements),
        cap=cap,
    )


# --- resolve_in -------------------------------------------------------------------------------


def test_resolve_in_without_runtime_id_never_resolves_to_another_element() -> None:
    """An empty RuntimeId is not an identity: it must not match a different element that also
    lacks one, even when role and name agree (design 5.9: resolution is by RuntimeId)."""
    snapshot = _snap(_el(1, "Button", "Send", ()))
    fresh = _snap(_el(7, "Button", "Send", ()), snapshot_id="s2")
    with pytest.raises(SteerError) as info:
        resolve_in(snapshot, 1, fresh)
    assert info.value.code is ErrorCode.ELEMENT_NOT_FOUND


def test_resolve_in_without_runtime_id_does_not_report_a_bogus_change() -> None:
    snapshot = _snap(_el(1, "Button", "Send", ()))
    fresh = _snap(_el(1, "Text", "hello", ()), _el(2, "Button", "Send", ()), snapshot_id="s2")
    with pytest.raises(SteerError, match="element_not_found") as info:
        resolve_in(snapshot, 1, fresh)
    assert "changed from" not in info.value.message


def test_resolve_in_against_the_same_snapshot_returns_the_element_itself() -> None:
    chosen = _el(2, "Button", "Send", (5, 1))
    snapshot = _snap(_el(1, "Edit", "Message", (4, 1)), chosen)
    assert resolve_in(snapshot, 2, snapshot) is chosen


def test_resolve_in_rejects_a_role_change_with_the_same_name() -> None:
    snapshot = _snap(_el(1, "Button", "Send", (5, 1)))
    fresh = _snap(_el(1, "Text", "Send", (5, 1)), snapshot_id="s2")
    with pytest.raises(SteerError, match="element_not_found: element 1 changed from Button"):
        resolve_in(snapshot, 1, fresh)


def test_resolve_in_reports_gone_when_runtime_id_is_absent_from_fresh() -> None:
    snapshot = _snap(_el(1, "Button", "Send", (5, 1)))
    fresh = _snap(_el(1, "Button", "Send", (6, 1)), snapshot_id="s2")
    with pytest.raises(SteerError, match="element_not_found: element 1 of s1 is gone"):
        resolve_in(snapshot, 1, fresh)


def test_resolve_in_with_duplicate_runtime_ids_in_fresh_takes_the_first_in_tree_order() -> None:
    """Documents current behaviour: the first RuntimeId match wins, so a later candidate with the
    right role and name does not rescue a changed first one."""
    snapshot = _snap(_el(1, "Button", "Send", (5,)))
    fresh = _snap(
        _el(1, "Button", "Cancel", (5,)), _el(2, "Button", "Send", (5,)), snapshot_id="s2"
    )
    with pytest.raises(SteerError, match="changed from Button 'Send' to Button 'Cancel'"):
        resolve_in(snapshot, 1, fresh)


# --- diff -------------------------------------------------------------------------------------


def test_diff_of_two_empty_snapshots_is_empty() -> None:
    d = diff(_snap(), _snap(snapshot_id="s2"))
    assert d.appeared == []
    assert d.disappeared == []


def test_diff_ignores_elements_without_a_runtime_id() -> None:
    before = _snap(_el(1, "Text", "a", ()))
    after = _snap(_el(1, "Text", "b", ()), _el(2, "Button", "c", ()), snapshot_id="s2")
    d = diff(before, after)
    assert d.appeared == []
    assert d.disappeared == []


def test_diff_with_duplicate_runtime_ids_in_before_hides_one_disappearance() -> None:
    """Documents current set semantics: two rows sharing a RuntimeId collapse to one."""
    before = _snap(_el(1, "Text", "a", (1,)), _el(2, "Text", "a", (1,)))
    after = _snap(_el(1, "Text", "a", (1,)), snapshot_id="s2")
    assert diff(before, after).disappeared == []


def test_diff_of_a_snapshot_with_itself_is_empty() -> None:
    snapshot = _snap(_el(1, "Text", "a", (1,)), _el(2, "Button", "b", (2,)))
    d = diff(snapshot, snapshot)
    assert d.appeared == []
    assert d.disappeared == []


def test_diff_lists_appeared_and_disappeared_in_tree_order() -> None:
    before = _snap(_el(1, "Text", "a", (1,)), _el(2, "Text", "b", (2,)), _el(3, "Text", "c", (3,)))
    after = _snap(_el(1, "Text", "x", (9,)), _el(2, "Text", "b", (2,)), _el(3, "Text", "y", (8,)))
    d = diff(before, after)
    assert [e.runtime_id for e in d.appeared] == [(9,), (8,)]
    assert [e.runtime_id for e in d.disappeared] == [(1,), (3,)]


# --- Snapshot ---------------------------------------------------------------------------------


def test_snapshot_lines_with_cap_zero_raises() -> None:
    with pytest.raises(ValueError, match="cap must be >= 1"):
        _ = _snap(_el(1, "Button", "a"), cap=0).lines


def test_snapshot_lines_with_cap_one_keeps_the_first_and_counts_the_rest() -> None:
    snapshot = _snap(_el(1, "Button", "a"), _el(2, "Button", "b"), cap=1)
    assert snapshot.lines == ['1 Button "a" [0,0,10,10]', "(... 1 more not shown)"]


def test_snapshot_lines_with_cap_equal_to_eligible_has_no_remainder_note() -> None:
    snapshot = _snap(_el(1, "Button", "a"), _el(2, "Button", "b"), cap=2)
    assert snapshot.lines == ['1 Button "a" [0,0,10,10]', '2 Button "b" [0,0,10,10]']


def test_snapshot_lines_of_an_empty_snapshot_is_empty() -> None:
    assert _snap().lines == []


def test_snapshot_line_ids_survive_pruning_so_they_map_back_to_elements() -> None:
    snapshot = _snap(
        _el(1, "Group", ""), _el(2, "Button", "b", offscreen=True), _el(3, "Button", "c")
    )
    assert snapshot.lines == ['3 Button "c" [0,0,10,10]']
    assert snapshot.element(3).name == "c"


def test_snapshot_element_accepts_ids_the_chooser_never_saw() -> None:
    """Documents current behaviour: an offscreen or capped-out element still resolves by id."""
    snapshot = _snap(_el(1, "Button", "a"), _el(2, "Button", "b", offscreen=True), cap=1)
    assert snapshot.lines == ['1 Button "a" [0,0,10,10]']
    assert snapshot.element(2).offscreen is True


def test_snapshot_element_on_empty_snapshot_raises_element_not_found() -> None:
    with pytest.raises(SteerError, match="element_not_found: no element 1 in s1"):
        _snap().element(1)


def test_snapshot_handle_returns_the_aligned_handle() -> None:
    snapshot = Snapshot(
        id="s1",
        hwnd=HWND,
        process="p",
        title="t",
        taken_at=0.0,
        elements=[_el(1, "Button", "a"), _el(2, "Button", "b")],
        node_count=2,
        stable=True,
        handles=["h1", "h2"],
    )
    assert snapshot.handle(2) == "h2"
    with pytest.raises(SteerError, match="element_not_found: no element 3 in s1"):
        snapshot.handle(3)


def test_snapshot_handle_with_misaligned_handles_raises_element_not_found() -> None:
    # Edge case WM-28: a snapshot rebuilt without live handles cannot act.
    snapshot = Snapshot(
        id="s1",
        hwnd=HWND,
        process="p",
        title="t",
        taken_at=0.0,
        elements=[_el(1, "Button", "a")],
        node_count=1,
        stable=True,
        handles=[],
    )
    with pytest.raises(SteerError, match="holds no live handles"):
        snapshot.handle(1)


def test_snapshot_fingerprint_of_dynamic_only_elements_has_only_the_title_mark() -> None:
    snapshot = _snap(_el(1, "ListItem", "row"), _el(2, "Text", "t"), _el(3, "Window", "w"))
    assert snapshot.fingerprint.marks == frozenset({("title", "t")})
    assert snapshot.fingerprint.process == "test.exe"


# --- observe ----------------------------------------------------------------------------------


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def time(self) -> float:
        return 1_700_000_000.0


class _FakeClient:
    """Serves one TreeSnapshot per call from `trees`; the last one repeats if calls exceed it."""

    dpi_per_monitor_v2 = True

    def __init__(self, trees: list[TreeSnapshot]) -> None:
        self.trees = trees
        self.calls: list[int] = []

    def cached_tree(self, hwnd: int) -> TreeSnapshot:
        self.calls.append(hwnd)
        index = min(len(self.calls) - 1, len(self.trees) - 1)
        return self.trees[index]


def _node(role: str, name: str, rid: tuple[int, ...]) -> RawNode:
    return RawNode(
        depth=0, role=role, name=name, automation_id="", box=BOX, offscreen=False, runtime_id=rid
    )


def _tree(count: int) -> TreeSnapshot:
    nodes = [_node("Button", f"b{i}", (i,)) for i in range(1, count + 1)]
    handles = [f"h{i}" for i in range(1, count + 1)]
    return TreeSnapshot(nodes=nodes, fetch_ms=1.0, walk_ms=0.1, handles=handles)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    fake = _FakeClock()
    fake_time = SimpleNamespace(monotonic=fake.monotonic, sleep=fake.sleep, time=fake.time)
    monkeypatch.setattr(model_module, "time", fake_time)
    for name, value in (
        ("is_window", lambda _h: True),
        ("is_cloaked", lambda _h: False),
        ("is_window_enabled", lambda _h: True),
        ("is_minimized", lambda _h: False),
        ("window_pid", lambda _h: 0),
        ("window_title", lambda _h: ""),
        ("process_name", lambda _pid: ""),
    ):
        monkeypatch.setattr(model_module, name, value)
    return fake


def _model(client: _FakeClient) -> WorldModel:
    # Structural fake: `observe` only calls `cached_tree`, so the cast is safe for the test.
    return WorldModel(cast(UiaClient, client))


def test_observe_is_stable_after_two_equal_runtime_id_sets(clock: _FakeClock) -> None:
    # Measured defaults [v1.0]: two fetches, 50 ms poll (design section 4).
    client = _FakeClient([_tree(5)])
    snapshot = _model(client).observe(HWND)
    assert snapshot.stable is True
    assert client.calls == [HWND, HWND]
    assert clock.sleeps == [0.05]
    assert snapshot.node_count == 5
    assert snapshot.taken_at == 1_700_000_000.0


def test_observe_counts_only_the_last_window_of_fetches(clock: _FakeClock) -> None:
    client = _FakeClient([_tree(1), _tree(2), _tree(3), _tree(3), _tree(3)])
    snapshot = _model(client).observe(HWND, cap_s=10.0, stable_window=3)
    assert snapshot.stable is True
    assert len(client.calls) == 5
    assert snapshot.node_count == 3


def test_observe_gives_up_at_cap_and_flags_unstable(clock: _FakeClock) -> None:
    growing = [_tree(i) for i in range(1, 50)]
    client = _FakeClient(growing)
    snapshot = _model(client).observe(HWND, cap_s=1.0, poll_s=0.25)
    assert snapshot.stable is False
    # fetches at t=0, 0.25, 0.5, 0.75; the sleep to t=1.0 hits the deadline: no late fetch (WM-34)
    assert len(client.calls) == 4
    assert clock.now == 1.0
    assert snapshot.node_count == 4


def test_observe_with_cap_zero_fetches_once_without_sleeping(clock: _FakeClock) -> None:
    client = _FakeClient([_tree(4)])
    snapshot = _model(client).observe(HWND, cap_s=0.0)
    assert client.calls == [HWND]
    assert clock.sleeps == []
    assert snapshot.stable is False
    assert snapshot.node_count == 4


def test_observe_with_stable_window_one_is_stable_after_a_single_fetch(clock: _FakeClock) -> None:
    client = _FakeClient([_tree(4), _tree(9)])
    snapshot = _model(client).observe(HWND, stable_window=1)
    assert client.calls == [HWND]
    assert clock.sleeps == []
    assert snapshot.stable is True


def test_observe_with_stable_window_two_needs_two_equal_sets(clock: _FakeClock) -> None:
    client = _FakeClient([_tree(4), _tree(6), _tree(6)])
    snapshot = _model(client).observe(HWND, stable_window=2)
    assert client.calls == [HWND, HWND, HWND]
    assert snapshot.stable is True
    assert snapshot.node_count == 6


def test_observe_never_fetches_after_the_deadline(clock: _FakeClock) -> None:
    # fetch at 0, sleep to 0.25 (< 0.4), fetch (streak 2 of 3), sleep to 0.5 >= 0.4: stop (WM-34)
    client = _FakeClient([_tree(5)])
    snapshot = _model(client).observe(HWND, cap_s=0.4, poll_s=0.25, stable_window=3)
    assert snapshot.stable is False
    assert len(client.calls) == 2
    assert clock.now == 0.5


def test_observe_deadline_equal_to_elapsed_stops_before_the_next_fetch(clock: _FakeClock) -> None:
    # fetch at 0, sleep to 0.25; 0.25 >= 0.25 so no second fetch
    client = _FakeClient([_tree(5)])
    snapshot = _model(client).observe(HWND, cap_s=0.25, poll_s=0.25, stable_window=3)
    assert snapshot.stable is False
    assert len(client.calls) == 1


def test_observe_of_an_empty_tree_is_never_stable(clock: _FakeClock) -> None:
    # Edge case WM-10: a tree below min_nodes (default 1) is first contact, not a settled screen.
    client = _FakeClient([_tree(0)])
    snapshot = _model(client).observe(HWND, cap_s=0.2)
    assert snapshot.stable is False
    assert snapshot.elements == []
    assert snapshot.lines == []
    assert snapshot.fingerprint.marks == frozenset()
    assert snapshot.node_count == 0


def test_observe_without_a_listed_window_has_empty_process_and_title(clock: _FakeClock) -> None:
    snapshot = _model(_FakeClient([_tree(1)])).observe(HWND)
    assert snapshot.process == ""
    assert snapshot.title == ""
    assert snapshot.hwnd == HWND


def test_observe_takes_process_and_title_from_the_window_handle(
    clock: _FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(model_module, "window_pid", lambda _h: 42)
    monkeypatch.setattr(model_module, "window_title", lambda _h: "Inbox")
    monkeypatch.setattr(model_module, "process_name", lambda pid: {42: "chrome.exe"}.get(pid, ""))
    snapshot = _model(_FakeClient([_tree(1)])).observe(HWND)
    assert snapshot.process == "chrome.exe"
    assert snapshot.title == "Inbox"
    assert snapshot.pid == 42
    assert snapshot.fingerprint.process == "chrome.exe"
    assert ("title", "inbox") in snapshot.fingerprint.marks


def test_observe_maps_nodes_to_elements_handles_and_cap(clock: _FakeClock) -> None:
    client = _FakeClient([_tree(3)])
    snapshot = _model(client).observe(HWND, cap=2)
    assert [e.id for e in snapshot.elements] == [1, 2, 3]
    assert [e.runtime_id for e in snapshot.elements] == [(1,), (2,), (3,)]
    assert snapshot.handle(3) == "h3"
    assert snapshot.cap == 2
    assert snapshot.lines[-1] == "(... 1 more not shown)"
    assert len(snapshot.id) == 12


def test_observe_twice_yields_distinct_ids_and_resolves_across_them(clock: _FakeClock) -> None:
    model = _model(_FakeClient([_tree(2)]))
    first = model.observe(HWND)
    second = model.observe(HWND)
    assert first.id != second.id
    assert model.resolve(first, 2, second).runtime_id == (2,)
    assert model.diff(first, second).appeared == []


class _FlakyClient(_FakeClient):
    """Fails the first `failures` fetches the way a UIA provider does right after a foreground
    change (E_FAIL, measured 2026-11-04 through the overlay: one retry 0.5 s later succeeded)."""

    def __init__(self, trees: list[TreeSnapshot], failures: int) -> None:
        super().__init__(trees)
        self.failures = failures

    def cached_tree(self, hwnd: int) -> TreeSnapshot:
        if self.failures > 0:
            self.failures -= 1
            self.calls.append(hwnd)
            raise OSError("uia fetch failed for hwnd 0x10: E_FAIL")
        return super().cached_tree(hwnd)


def test_a_transient_fetch_failure_is_retried_within_the_observe(clock: _FakeClock) -> None:
    client = _FlakyClient([_tree(5)], failures=1)
    snapshot = _model(client).observe(HWND)
    assert snapshot.stable is True
    assert client.calls == [HWND, HWND, HWND], "one failed fetch, then the two stable ones"
    assert model_module.FETCH_RETRY_S in clock.sleeps


def test_a_fetch_that_keeps_failing_is_perception_timeout_after_the_retries(
    clock: _FakeClock,
) -> None:
    client = _FlakyClient([_tree(5)], failures=99)
    with pytest.raises(SteerError) as caught:
        _model(client).observe(HWND)
    assert caught.value.code is ErrorCode.PERCEPTION_TIMEOUT
    assert len(client.calls) == model_module.FETCH_RETRIES + 1
