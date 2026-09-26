"""UIA cached snapshot: one cross-process round trip, timed, with timeouts set."""

import pytest

from steerai.probes.uia_snapshot import Snapshot, UiaClient
from steerai.probes.windows import list_top_windows

pytestmark = pytest.mark.windows


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


def test_bounding_rect_of_a_real_window_is_non_empty(client: UiaClient) -> None:
    hwnd = list_top_windows()[0].hwnd
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
