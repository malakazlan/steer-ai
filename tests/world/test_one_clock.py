"""[V2.40] Q2, adapter slice 1: one clock from the read to the act. `WorldModel` stamps
`Snapshot.taken_at` on an injected clock and the executors' admission ages a snapshot on theirs;
the adapter passes one monotonic clock to both. v1's default stays wall time, unchanged."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import cast

import pytest

from steerai.action.execution import UiaBindings
from steerai.action.executor import Action, Executor
from steerai.probes.uia_snapshot import RawNode, TreeSnapshot, UiaClient
from steerai.world import model as model_module
from steerai.world.model import WorldModel, snapshot_to_json
from tests.action.fakes import BOX, TYPE_IDS, FakeHandle, FakeWindows

HWND = 0x10
WALL = 1_700_000_000.0
MONO = 5_000.0
"""A monotonic reading far from wall time, as `time.monotonic` is."""


class _Client:
    """Serves one cached tree: a single Button whose live handle is a `FakeHandle`."""

    dpi_per_monitor_v2 = True

    def __init__(self, handle: FakeHandle) -> None:
        self._handle = handle

    def cached_tree(self, hwnd: int) -> TreeSnapshot:  # noqa: ARG002 - the client's shape
        node = RawNode(
            depth=0,
            role="Button",
            name="Send",
            automation_id="",
            box=BOX,
            offscreen=False,
            runtime_id=(7, 1),
        )
        return TreeSnapshot(nodes=[node], fetch_ms=1.0, walk_ms=0.1, handles=[self._handle])


@pytest.fixture
def window(monkeypatch: pytest.MonkeyPatch) -> None:
    """The window checks pass, and the model's own `time` is fake: wall time reads `WALL`."""
    fake_time = SimpleNamespace(monotonic=lambda: 0.0, sleep=lambda _s: None, time=lambda: WALL)
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


def _executor(clock: float) -> Executor:
    return Executor(
        uia=UiaBindings(control_types={v: k for k, v in TYPE_IDS.items()}, interfaces={}),
        windows=FakeWindows(),
        clock=lambda: clock,
    )


@pytest.mark.usefixtures("window")
def test_a_snapshot_is_stamped_on_the_injected_clock() -> None:
    snapshot = WorldModel(cast(UiaClient, _Client(FakeHandle())), clock=lambda: MONO).observe(HWND)
    assert snapshot.taken_at == MONO


@pytest.mark.usefixtures("window")
def test_the_default_clock_is_still_wall_time() -> None:
    """v1 is byte-identical: without a clock, `taken_at` is `time.time()` as before."""
    snapshot = WorldModel(cast(UiaClient, _Client(FakeHandle()))).observe(HWND)
    assert snapshot.taken_at == WALL


@pytest.mark.usefixtures("window")
def test_one_clock_runs_from_the_read_to_admission() -> None:
    """Read and act on one monotonic clock: the snapshot is young, admitted and acted on."""
    handle = FakeHandle()
    snapshot = WorldModel(cast(UiaClient, _Client(handle)), clock=lambda: MONO).observe(HWND)
    result = _executor(MONO + 1.0).execute(
        Action(verb="click", element_id=snapshot.elements[0].id), snapshot
    )
    assert result.evidence.get("detail") != "snapshot_stale"
    assert handle.invoked == 1


@pytest.mark.usefixtures("window")
def test_two_clocks_make_every_act_stale() -> None:
    """Why the clock is injected into both: a monotonic `taken_at` aged on wall time reads as
    decades old, so admission refuses every act (`snapshot_stale`)."""
    handle = FakeHandle()
    snapshot = WorldModel(cast(UiaClient, _Client(handle)), clock=lambda: MONO).observe(HWND)
    result = _executor(WALL).execute(
        Action(verb="click", element_id=snapshot.elements[0].id), snapshot
    )
    assert result.evidence.get("detail") == "snapshot_stale"
    assert handle.invoked == 0


@pytest.mark.usefixtures("window")
def test_a_monotonic_reading_is_never_persisted() -> None:
    """[V2.40] Q2: monotonic time is for durations and freshness only; what is persisted carries
    wall time. The write-ahead's snapshot JSON holds the wall reading, never the monotonic one."""
    snapshot = WorldModel(cast(UiaClient, _Client(FakeHandle())), clock=lambda: MONO).observe(HWND)
    assert (snapshot.taken_at, snapshot.wall_at) == (MONO, WALL)
    assert json.loads(snapshot_to_json(snapshot))["taken_at"] == WALL


V1_JSON = (
    '{"version":2,"id":"SNAPSHOT_ID","hwnd":16,"process":"","title":"","taken_at":1700000000.0,'
    '"node_count":1,"stable":true,"minimized":false,"pid":0,"cap":150,"elements":[{"id":1,'
    '"depth":0,"role":"Button","name":"Send","box":[10,10,110,50],"offscreen":false,'
    '"enabled":true,"automation_id":"","runtime_id":[7,1],"parent_id":null,"secure":false,'
    '"caps":[]}]}'
)
"""The write-ahead JSON of this fixture as main printed it before this change (origin/main
d96f2a9, 2026-11-15). Only the snapshot `id` is random per snapshot, so it is filled in."""


@pytest.mark.usefixtures("window")
def test_v1_snapshots_persist_exactly_as_before() -> None:
    snapshot = WorldModel(cast(UiaClient, _Client(FakeHandle()))).observe(HWND)
    assert snapshot.wall_at is None
    assert snapshot_to_json(snapshot) == V1_JSON.replace("SNAPSHOT_ID", snapshot.id)


@pytest.mark.usefixtures("window")
def test_a_v2_snapshot_persists_in_the_same_format_with_wall_time() -> None:
    """The injected clock changes no key: `taken_at` holds the wall reading, and no `wall_at`
    or monotonic field is added to the record."""
    snapshot = WorldModel(cast(UiaClient, _Client(FakeHandle())), clock=lambda: MONO).observe(HWND)
    assert snapshot_to_json(snapshot) == V1_JSON.replace("SNAPSHOT_ID", snapshot.id)
