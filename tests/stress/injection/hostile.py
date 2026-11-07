"""Shared parts of the injection suite: a hostile page over the loop's scripted world, a dispatch
log that records what each execution touched, a typist at the console, and the one trace scanner
every run passes through (IJ-29)."""

from __future__ import annotations

import io
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from steerai.action.dispositions import ActionResult
from steerai.orchestrator.gate import Confirmer, Gate
from steerai.orchestrator.loop import Session
from steerai.world.model import Snapshot
from tests.orchestrator.fakes import FakeExecutor, FakeLive, FakeWorld
from tests.orchestrator.test_loop import Harness
from tests.stress.injection.catalogue import sentinels


@dataclass
class HostilePage:
    """The scripted world as a hostile page shows it: automation ids by element name, and a
    title the page owns."""

    world: FakeWorld
    automation_ids: Mapping[str, str] = field(default_factory=dict)
    title: str | None = None

    def observe(self, hwnd: int, **kwargs: Any) -> Snapshot:
        snapshot = self.world.observe(hwnd, **kwargs)
        elements = [
            replace(e, automation_id=self.automation_ids[e.name])
            if e.name in self.automation_ids
            else e
            for e in snapshot.elements
        ]
        title = snapshot.title if self.title is None else self.title
        return replace(snapshot, elements=elements, title=title)


@dataclass(frozen=True, slots=True)
class Dispatch:
    """One execution as the log shows it: the executor verb and the element it touched."""

    verb: str
    role: str
    name: str
    runtime_id: tuple[int, ...]


@dataclass
class RecordingExecutor:
    """Logs the element each execution touches, then lets the scripted executor act."""

    inner: FakeExecutor
    log: list[Dispatch]

    def execute(self, action: Any, snapshot: Snapshot) -> ActionResult:
        element = snapshot.element(action.element_id)
        self.log.append(Dispatch(action.verb, element.role, element.name, element.runtime_id))
        return self.inner.execute(action, snapshot)


def rebuild(
    h: Harness,
    *,
    confirmer: Confirmer | None = None,
    observer: Any | None = None,  # Any: a FakeWorld or a HostilePage, both observe like one
    log: list[Dispatch] | None = None,
) -> None:
    """The harness's session again over a different confirmer or observer; with a log, every
    execution is recorded first."""
    semantic: Any = h.semantic  # Any: a FakeExecutor or its recording wrapper
    physical: Any = h.physical
    if log is not None:
        semantic = RecordingExecutor(h.semantic, log)
        physical = RecordingExecutor(h.physical, log)
    h.session = Session(
        task_id="task-1",
        hwnd=0x10,
        observer=h.world if observer is None else observer,
        semantic=semantic,
        physical=physical,
        gate=Gate(
            confirmer=h.confirmer if confirmer is None else confirmer,
            own_pid=4242,
            task_id="task-1",
        ),
        ledger=h.ledger,
        live=FakeLive(h.world),
        trace=h.trace,
        monotonic=lambda: h.clock["t"],
        sleep=lambda s: h.clock.__setitem__("t", h.clock["t"] + s),
        verify_deadline_s=0.5,
    )


class Typist(io.StringIO):
    """The console's screen with a person in front of it: each ask the console writes is
    answered by typing `line` into `stdin` (with `{id}` filled in from the ask's own line), and
    nothing at all is typed for silence."""

    def __init__(self, line: str | None) -> None:
        super().__init__()
        self.stdin = io.StringIO()
        self._line = line

    def write(self, s: str, /) -> int:
        written = super().write(s)
        for row in s.splitlines():
            if self._line is not None and row.startswith("ASK "):
                unread = self.stdin.tell()
                self.stdin.seek(0, io.SEEK_END)
                self.stdin.write(self._line.replace("{id}", row.split()[1]) + "\n")
                self.stdin.seek(unread)
        return written


def scan_trace(lines: Sequence[str], payloads: Sequence[str] | None = None) -> list[str]:
    """The payloads found in any trace line, raw or as JSON escapes them: empty when the trace
    is clean (5.14; IJ-29)."""
    wanted = sentinels() if payloads is None else tuple(payloads)
    joined = "\n".join(lines)
    return [
        payload
        for payload in wanted
        if payload in joined or json.dumps(payload, ensure_ascii=False)[1:-1] in joined
    ]
