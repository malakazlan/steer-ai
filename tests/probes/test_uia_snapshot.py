"""UIA cached snapshot: one cross-process round trip, timed, with timeouts set."""

import ctypes

import pytest

from steerai.probes.uia_snapshot import Snapshot, UiaClient
from steerai.probes.win_input import window_rect
from steerai.probes.windows import list_top_windows

pytestmark = pytest.mark.windows


def _drivable_hwnd() -> int:
    """A visible, non-minimized window with a real rectangle, or skip when the desktop has none."""
    for window in list_top_windows():
        if window.minimized:
            continue
        left, top, right, bottom = window_rect(window.hwnd)
        if right > left and bottom > top:
            return window.hwnd
    pytest.skip("every window on this desktop is minimized")


@pytest.fixture(scope="module")
def client() -> UiaClient:
    return UiaClient(connection_timeout_ms=5000, transaction_timeout_ms=5000)


def test_desktop_children_snapshot_counts_windows(client: UiaClient) -> None:
    snapshot = client.snapshot_desktop_children()
    assert isinstance(snapshot, Snapshot)
    assert snapshot.node_count >= 1
    assert snapshot.elapsed_ms > 0.0


def test_subtree_snapshot_of_a_real_window(client: UiaClient) -> None:
    hwnd = list_top_windows()[0].hwnd
    snapshot = client.snapshot_subtree(hwnd)
    assert snapshot.node_count >= 1
    assert snapshot.elapsed_ms > 0.0
    assert sum(snapshot.by_control_type.values()) == snapshot.node_count


def test_cached_tree_has_structure_and_matches_flat_count(client: UiaClient) -> None:
    hwnd = list_top_windows()[0].hwnd
    tree = client.cached_tree(hwnd)
    assert tree.nodes[0].depth == 0
    pairs = zip(tree.nodes, tree.nodes[1:], strict=False)
    assert all(0 <= child.depth <= parent.depth + 1 for parent, child in pairs)
    assert tree.fetch_ms > 0
    assert len(tree.nodes) == client.snapshot_subtree(hwnd).node_count


def test_bounding_rect_of_a_real_window_is_non_empty(client: UiaClient) -> None:
    hwnd = _drivable_hwnd()
    left, top, right, bottom = client.bounding_rect(hwnd)
    assert right > left
    assert bottom > top


def test_client_made_process_per_monitor_v2_aware(client: UiaClient) -> None:
    assert client.dpi_per_monitor_v2 is True


def test_timeouts_are_applied(client: UiaClient) -> None:
    assert client.connection_timeout_ms == 5000
    assert client.transaction_timeout_ms == 5000


def test_invalid_hwnd_raises(client: UiaClient) -> None:
    with pytest.raises(OSError, match="no UIA element for hwnd"):
        client.snapshot_subtree(0x7FFF_FFF1)


def test_cached_tree_handles_align_with_nodes_and_runtime_ids_are_set(client: UiaClient) -> None:
    hwnd = _drivable_hwnd()
    tree = client.cached_tree(hwnd)
    assert len(tree.handles) == len(tree.nodes)
    assert all(node.runtime_id for node in tree.nodes)
    assert all(isinstance(node.enabled, bool) for node in tree.nodes)


def test_cached_tree_runtime_ids_are_unique_within_one_snapshot(client: UiaClient) -> None:
    tree = client.cached_tree(_drivable_hwnd())
    ids = [node.runtime_id for node in tree.nodes]
    assert len(ids) == len(set(ids))


def test_cached_tree_of_an_invalid_hwnd_raises(client: UiaClient) -> None:
    with pytest.raises(OSError, match="no UIA element for hwnd"):
        client.cached_tree(0x7FFF_FFF1)


def test_runtime_id_at_a_window_centre_is_an_element_of_that_window(client: UiaClient) -> None:
    window = next(w for w in list_top_windows() if w.title and not w.minimized)
    left, top, right, bottom = window_rect(window.hwnd)
    tree = client.cached_tree(window.hwnd)
    runtime_id = client.runtime_id_at((left + right) // 2, (top + bottom) // 2)
    assert runtime_id, "an element exists under every visible window pixel"
    known = {node.runtime_id for node in tree.nodes}
    assert runtime_id in known or runtime_id == (), (runtime_id, window.title)


def test_runtime_id_at_a_point_outside_every_monitor_is_the_desktop_root(
    client: UiaClient,
) -> None:
    # Measured 2026-11-04 (Win10 22H2 and the windows-latest runner): ElementFromPoint never
    # answers "nothing"; off every monitor it returns the desktop root, whose RuntimeId is
    # (42, <desktop window handle>): 0x10010 here, 0x1000C on the runner. The covered check
    # compares against the target subtree, so the root can never pass it.
    desktop = int(ctypes.windll.user32.GetDesktopWindow())
    assert client.runtime_id_at(-100_000, -100_000) == (42, desktop)
