"""[V2.27] A: the v2-path shadow diagnostic. v1 drives; around each step the v2 full read
(`read_observed`) is taken before and after through v1's own debug channel and trace, and
`effects.observe` compares them. Nothing is scored, and v1's results and trace are byte for
byte the same with the shadow on. Fakes only: no UIA, no browser."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest

import steerai.run as run_mod
from steerai.debug_trace import NullDebugSink
from steerai.errors import ErrorCode, SteerError
from steerai.orchestrator.gate import Gate
from steerai.orchestrator.loop import Session
from steerai.orchestrator.trace import ListTrace
from steerai.probes.shadow_v2 import ShadowDebug, ShadowTrace, V2Shadow, installed, summary
from steerai.run import Bound, Deps, LedgerLike
from steerai.world.address_bar import DocumentUrl
from steerai.world.elements import Element
from steerai.world.landmarks import PageFacts
from steerai.world.model import Snapshot
from tests.orchestrator.fakes import FakeConfirmer, FakeLive
from tests.orchestrator.test_loop import Harness, _send, _type
from tests.orchestrator.test_run import _options, _plan
from tests.world.test_effects_conditions import BASE, FIELD, Row, _snap

KEY = b"k" * 32
URL = "https://shop.test/"
PAGE2: Row = (1, "Document", "Results", (9, 77), None)
NOTE: Row = (1, "Edit", "Text editor", (9, 1), None)


@dataclass
class Frame:
    """What one fresh observe sees: the snapshot and the facts read with it."""

    snapshot: Snapshot
    document_url: str | None = URL
    bar: str | None = "shop.test/"
    focus: tuple[int, ...] = ()
    values: dict[int, str] = field(default_factory=dict)
    fails: bool = False


class FakeReads:
    """The shadow's reads: each `observe` takes the next frame; the facts read that frame."""

    def __init__(self, frames: list[Frame]) -> None:
        self.frames = frames
        self.now: Frame | None = None
        self.value_reads: list[int] = []
        self.page_fact_reads = 0

    @property
    def facts(self) -> FakeReads:
        return self

    def observe(self, hwnd: int) -> Snapshot:  # noqa: ARG002 - the reader's shape
        self.now = self.frames.pop(0)
        if self.now.fails:
            raise SteerError(ErrorCode.APP_CRASHED, "window gone")
        return self.now.snapshot

    def windows(self, pid: int) -> tuple[int, ...]:  # noqa: ARG002 - the reader's shape
        return (0x10,)

    def value(self, snapshot: Snapshot, element_id: int) -> str | None:  # noqa: ARG002
        assert self.now is not None
        self.value_reads.append(element_id)
        return self.now.values.get(element_id)

    def document_url_in(self, snapshot: Snapshot, document_id: int) -> DocumentUrl | None:  # noqa: ARG002
        assert self.now is not None
        url = self.now.document_url
        return None if url is None else DocumentUrl(runtime_id=(9, 1), url=url)

    def address_bar(self, hwnd: int) -> str | None:  # noqa: ARG002 - the reader's shape
        assert self.now is not None
        return self.now.bar

    def focused_runtime_id(self) -> tuple[int, ...]:
        assert self.now is not None
        return self.now.focus

    def page_facts(self, hwnd: int) -> PageFacts:  # noqa: ARG002 - the reader's shape
        self.page_fact_reads += 1
        return PageFacts(roles={}, headings={}, landmarks={})


EDITABLE: dict[int, tuple[str, ...]] = {FIELD[0]: ("editable",)}


def _page(rows: list[Row] = BASE, *, secure: bool = False) -> Snapshot:
    snapshot = _snap(rows, caps=EDITABLE)
    if secure:
        snapshot.elements[1] = _secure(snapshot)
    return snapshot


def _secure(snapshot: Snapshot) -> Element:
    return replace(snapshot.elements[1], secure=True)


def _step_row(step_id: str, verb: str, delivery: str = "delivered_verified") -> dict[str, Any]:
    return {
        "kind": "step",
        "task_id": "suite-g1-1-abcd1234",
        "step_id": step_id,
        "ok": True,
        "action": {"verb": verb, "delivery": delivery},
    }


def _run(
    frames: list[Frame], acted: tuple[int, ...], verb: str, *, effect: bool = True
) -> tuple[dict[str, Any], FakeReads]:
    rows: list[dict[str, Any]] = []
    reads = FakeReads(frames)
    shadow = V2Shadow(reads, hwnd=0x10, key=KEY, emit=rows.append)
    shadow.before()
    if effect:
        shadow.after(acted)
    shadow.finish(_step_row("s1", verb))
    (row,) = rows
    return row, reads


def test_a_verified_type_gives_own_value_on_the_v2_path() -> None:
    row, _ = _run(
        [Frame(_page(), values={2: ""}), Frame(_page(), values={2: "red shoes"})],
        FIELD[3],
        "type",
    )
    assert row["own_value"] is True
    assert "own_value" in row["classes"]
    assert row["acted_value_read"] == [True, True]
    assert (row["task"], row["step_id"], row["verb"]) == ("g1", "s1", "type")
    assert row["delivery"] == "delivered_verified"
    assert "red shoes" not in json.dumps(row)


def test_a_verified_focus_gives_focus_on_the_v2_path() -> None:
    row, _ = _run([Frame(_page(), focus=(9, 1)), Frame(_page(), focus=FIELD[3])], FIELD[3], "focus")
    assert row["focus"] is True
    assert row["focus_read"] == [True, True]


def test_a_url_not_read_after_an_enter_is_unread_and_counted() -> None:
    row, _ = _run(
        [Frame(_page()), Frame(_page([PAGE2, *BASE[1:]]), document_url=None)], (), "press"
    )
    assert row["url_read"] == [True, False]
    assert "url" in row["unread"]
    assert row["unknown"] is False


def test_a_window_without_a_document_reads_no_page_facts_and_no_url_is_unread() -> None:
    note = _snap([NOTE])
    row, reads = _run(
        [Frame(note, document_url=None, bar=None), Frame(note, document_url=None, bar=None)],
        (9, 1),
        "type",
    )
    assert reads.page_fact_reads == 0
    assert "url" not in row["unread"]


def test_a_secure_field_value_is_never_read() -> None:
    _, reads = _run(
        [Frame(_page(secure=True), values={2: "x"}), Frame(_page(secure=True), values={2: "y"})],
        FIELD[3],
        "type",
    )
    assert reads.value_reads == []


def test_a_failed_read_is_a_token_in_the_row_never_an_exception() -> None:
    row, _ = _run([Frame(_page(), fails=True), Frame(_page())], FIELD[3], "type")
    assert row["errors"] == ["before:SteerError"]
    assert row["effect_row"] is True, "v1 logged its effect; only the shadow's read failed"
    assert row["classes"] is None


def test_a_step_with_no_effect_row_is_still_reported() -> None:
    row, _ = _run([Frame(_page())], FIELD[3], "click", effect=False)
    assert row["effect_row"] is False
    assert row["classes"] is None


def test_the_summary_counts_own_value_focus_unknown_unread_and_the_url_after_enter() -> None:
    rows: list[dict[str, Any]] = [
        {"verb": "type", "delivery": "delivered_verified", "classes": ["own_value"],
         "unread": [], "url_read": [True, True]},
        {"verb": "type", "delivery": "delivered_verified", "classes": ["none"],
         "unread": [], "url_read": [True, True]},
        {"verb": "focus", "delivery": "delivered_verified", "classes": ["focus"],
         "unread": [], "url_read": [True, True]},
        {"verb": "press", "delivery": "delivered_unverified", "classes": ["navigated"],
         "unread": ["url"], "url_read": [True, False]},
        {"verb": "click", "delivery": "delivered_unverified", "classes": ["unknown"],
         "unread": ["windows", "caps"], "url_read": [True, True]},
        {"verb": "click", "delivery": None, "classes": None, "unread": None, "url_read": None},
    ]  # fmt: skip
    lines = summary(rows)
    assert "verified type: 2, own_value 1" in lines
    assert "verified focus: 1, focus 1" in lines
    assert "unknown: 1 of 5 observed" in lines
    assert "unread: url 1, windows 1, caps 1 (of 5 observed)" in lines
    assert "press: 1, url unread 1, url not read after 1, navigated 1" in lines
    assert "no report: 1" in lines


# ---- v1 is the same with the shadow on ---------------------------------------------------------


def _shadowed(h: Harness, shadow: V2Shadow) -> Session:
    return Session(
        task_id="task-1",
        hwnd=0x10,
        observer=h.world,
        semantic=h.semantic,
        physical=h.physical,
        gate=Gate(confirmer=h.confirmer, own_pid=4242, task_id="task-1"),
        ledger=h.ledger,
        live=FakeLive(h.world),
        trace=ShadowTrace(h.trace, shadow),
        monotonic=lambda: h.clock["t"],
        sleep=lambda s: h.clock.__setitem__("t", h.clock["t"] + s),
        verify_deadline_s=0.5,
        debug=ShadowDebug(NullDebugSink(), shadow),
    )


def test_v1_results_and_trace_are_identical_with_the_shadow_on() -> None:
    plain = Harness()
    plain_results = [plain.session.run_step(s).to_dict() for s in (_type(), _send())]
    shadowed = Harness()
    rows: list[dict[str, Any]] = []
    frames = [Frame(_page()) for _ in range(4)]
    shadow = V2Shadow(FakeReads(frames), hwnd=0x10, key=KEY, emit=rows.append)
    shadowed.session = _shadowed(shadowed, shadow)
    results = [shadowed.session.run_step(s).to_dict() for s in (_type(), _send())]
    assert results == plain_results
    assert shadowed.trace.lines == plain.trace.lines
    assert [r["step_id"] for r in rows] == ["s2", "s3"]
    assert all(r["effect_row"] for r in rows)


class _Recording:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def begin_step(self, step_id: str) -> None:
        self.calls.append(("begin", step_id))

    def record(self, kind: str, fields: Mapping[str, Any]) -> None:
        self.calls.append(("record", kind, dict(fields)))

    def capture(self, kind: str, build: Any) -> None:  # Any: the sink's builder
        self.calls.append(("capture", kind, build))

    def flush(self) -> None:
        self.calls.append(("flush",))


def test_the_debug_proxy_passes_every_call_through_unchanged() -> None:
    inner = _Recording()
    rows: list[dict[str, Any]] = []
    shadow = V2Shadow(
        FakeReads([Frame(_page()), Frame(_page())]), hwnd=0x10, key=KEY, emit=rows.append
    )
    proxy = ShadowDebug(inner, shadow)

    def build() -> dict[str, Any]:
        return {"revealed_root": None, "acted": list(FIELD[3])}

    proxy.begin_step("s1")
    proxy.record("verify", {"holds": True})
    proxy.capture("effect", build)
    proxy.flush()
    assert inner.calls == [
        ("begin", "s1"),
        ("record", "verify", {"holds": True}),
        ("capture", "effect", build),
        ("flush",),
    ]


def test_the_trace_proxy_writes_every_record_unchanged() -> None:
    inner = ListTrace()
    shadow = V2Shadow(FakeReads([]), hwnd=0x10, key=KEY, emit=lambda _r: None)
    proxy = ShadowTrace(inner, shadow)
    records = [{"kind": "task", "n": 1}, _step_row("s1", "click")]
    for record in records:
        proxy.write(record)
    plain = ListTrace()
    for record in records:
        plain.write(record)
    assert inner.lines == plain.lines


def _no_ledger(_root: Path, _task: str) -> LedgerLike:
    raise AssertionError("the runner opens no ledger here")


def test_installed_shadows_each_session_the_runner_builds_then_restores_the_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[Bound] = []

    def build(bound: Bound) -> Any:  # Any: a session stand-in
        built.append(bound)
        return "session"

    def real_deps() -> Deps:
        return Deps(
            list_windows=list,
            exclusions=lambda: (set(), set()),
            acquire_foreground=str,
            open_ledger=_no_ledger,
            build_session=build,
        )

    monkeypatch.setattr(run_mod, "_real_deps", real_deps)
    rows: list[dict[str, Any]] = []
    made: list[V2Shadow] = []

    def make(bound: Bound) -> V2Shadow:
        made.append(V2Shadow(FakeReads([]), hwnd=bound.hwnd, key=KEY, emit=rows.append))
        return made[-1]

    trace, debug = ListTrace(), NullDebugSink()
    bound = Bound(
        hwnd=0x10,
        pid=1,
        title="Shop",
        task_id="t",
        options=_options(tmp_path, _plan(_type())),
        confirmer=FakeConfirmer(),
        ledger=None,
        trace=trace,
        debug=debug,
    )
    with installed(make):
        session: object = run_mod._real_deps().build_session(bound)
    assert session == "session"
    assert run_mod._real_deps is real_deps
    (seen,) = built
    assert isinstance(seen.trace, ShadowTrace)
    assert isinstance(seen.debug, ShadowDebug)
    assert (seen.hwnd, seen.task_id, seen.options) == (bound.hwnd, bound.task_id, bound.options)
    assert len(made) == 1
    seen.trace.write({"kind": "task"})
    assert trace.lines == ['{"kind": "task"}']
