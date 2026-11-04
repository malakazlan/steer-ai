"""The runner as a child process, driven by the app (design 5.1 asks surface; packet
launcher-2026-11-04, revised after the first live overlay runs).

`python -m steerai.run` is the proven path: its console prints a `GO` line for the plan preview
and an `ASK` block for a step, and reads the bound tokens on stdin. The app keeps that path
whole: it starts the runner as a child process, turns those blocks into `PendingAsk`s for the
window, writes the exact token the person's button means, and reads the final JSON line.
Nothing COM or UIA runs in the window's process (an in-process runner failed its second UIA
fetch whenever the Tk root existed before the runner thread, 2026-11-04, cause not found).
Closing the window terminates the child: the hooks and the task go with it.
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

ASK_PREFIX = "ASK "
GO_PREFIX = "  GO "
_ASK_END = "  the target window holds focus until you answer here"
_CANDIDATE_START = "  1) "


@dataclass(frozen=True, slots=True)
class Answer:
    """What the UI posts: typed, never free text."""

    kind: Literal["yes", "no", "choose", "go"]
    decision_id: str = ""
    ordinal: int = 0

    @classmethod
    def yes(cls, decision_id: str) -> Answer:
        return cls("yes", decision_id)

    @classmethod
    def no(cls) -> Answer:
        return cls("no")

    @classmethod
    def choose(cls, ordinal: int, decision_id: str) -> Answer:
        if ordinal < 1:
            raise ValueError("an ordinal counts from 1")
        return cls("choose", decision_id, ordinal)

    @classmethod
    def go(cls, go_id: str) -> Answer:
        return cls("go", go_id)


@dataclass(frozen=True, slots=True)
class PendingAsk:
    """What the UI shows while the runner waits."""

    kind: Literal["yes", "which", "go"]
    decision_id: str
    text: str
    risk: str
    candidates: int = 0


class RunnerProcess:
    def __init__(self, argv: Sequence[str], *, env: dict[str, str] | None = None) -> None:
        self._argv = list(argv)
        self._env = env
        self._proc: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._lines: list[str] = []
        self._pending: PendingAsk | None = None
        self._block: list[str] | None = None
        self._out: list[str] = []
        self.exit_code: int | None = None
        self.result: dict[str, Any] = {}
        self.killed = False
        self._events: queue.Queue[tuple[str, str | None]] = queue.Queue()

    # ---- lifecycle ---------------------------------------------------------------------------

    def start(self) -> None:
        self._proc = subprocess.Popen(  # noqa: S603 - the interpreter and a fixed module
            self._argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self._env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        threading.Thread(target=self._read_err, name="steer-runner-err", daemon=True).start()
        threading.Thread(target=self._read_out, name="steer-runner-out", daemon=True).start()
        threading.Thread(target=self._wait, name="steer-runner-wait", daemon=True).start()

    def running(self) -> bool:
        return self._proc is not None and self.exit_code is None

    def close(self) -> None:
        """Terminate the child: the task, its hooks and its asks end with it."""
        proc = self._proc
        if proc is None or self.exit_code is not None:
            return
        self.killed = True
        with self._lock:
            self._pending = None
            self._block = None
        proc.terminate()

    # ---- the UI side -------------------------------------------------------------------------

    def pending(self) -> PendingAsk | None:
        with self._lock:
            return self._pending

    def lines(self) -> list[str]:
        with self._lock:
            return list(self._lines)

    def drain_lines(self) -> list[str]:
        """New lines since the last drain, in order."""
        out: list[str] = []
        while True:
            try:
                kind, line = self._events.get_nowait()
            except queue.Empty:
                return out
            if kind == "line" and line is not None:
                out.append(line)

    def answer(self, answer: Answer) -> None:
        """Write the exact token the pending ask takes; anything else is dropped."""
        with self._lock:
            pending = self._pending
            token = None if pending is None else _token(answer, pending)
            if token is not None:
                self._pending = None
        if token is None:
            return
        proc = self._proc
        if proc is None or proc.stdin is None or self.exit_code is not None:
            return
        try:
            proc.stdin.write(token + "\n")
            proc.stdin.flush()
        except (OSError, ValueError):
            return

    # ---- the pipes ---------------------------------------------------------------------------

    def _read_err(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:  # start() sets both
            return
        for raw in proc.stderr:
            line = raw.rstrip("\r\n")
            with self._lock:
                self._lines.append(line)
                self._parse(line)
            self._events.put(("line", line))

    def _read_out(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:  # start() sets both
            return
        for raw in proc.stdout:
            self._out.append(raw.rstrip("\r\n"))

    def _wait(self) -> None:
        proc = self._proc
        if proc is None:  # start() sets it
            return
        code = proc.wait()
        for _ in range(50):  # the reader threads finish their last lines
            if proc.stdout is not None and proc.stdout.closed:
                break
            threading.Event().wait(0.02)
        last = next((line for line in reversed(self._out) if line.strip()), None)
        if last is not None:
            try:
                parsed = json.loads(last)
                if isinstance(parsed, dict):
                    self.result = parsed
            except ValueError:
                self.result = {}
        with self._lock:
            self._pending = None
            self._block = None
        self.exit_code = code

    def _parse(self, line: str) -> None:
        """Runs under the lock: a GO line is one ask; an ASK line opens a block that ends at the
        console's instruction line (the token to type, which the window never shows)."""
        if line.startswith(GO_PREFIX):
            go_id = line[len(GO_PREFIX) :].split(":", 1)[0].strip()
            self._pending = PendingAsk(kind="go", decision_id=go_id, text="", risk="plan")
            return
        if line.startswith(ASK_PREFIX):
            self._block = [line]
            return
        if self._block is None:
            return
        if line.startswith(_ASK_END):
            head = self._block[0].split()
            kind: Literal["yes", "which"] = (
                "which" if len(head) > 2 and head[2] == "which" else "yes"
            )
            risk = head[3] if len(head) > 3 else ""
            candidates = sum(1 for item in self._block if _is_candidate(item))
            self._pending = PendingAsk(
                kind=kind,
                decision_id=head[1] if len(head) > 1 else "",
                text="\n".join(self._block),
                risk=risk,
                candidates=candidates,
            )
            self._block = None
            return
        self._block.append(line)


def _is_candidate(line: str) -> bool:
    head = line.strip().split(")", 1)[0]
    return line.startswith("  ") and ")" in line and head.isdigit()


def _token(answer: Answer, pending: PendingAsk) -> str | None:
    if answer.kind == "no":
        return "no"
    if answer.decision_id != pending.decision_id:
        return None
    if answer.kind == "go" and pending.kind == "go":
        return f"go:{pending.decision_id}"
    if answer.kind == "yes" and pending.kind == "yes":
        return f"yes:{pending.decision_id}"
    in_range = 1 <= answer.ordinal <= pending.candidates
    if answer.kind == "choose" and pending.kind == "which" and in_range:
        return f"{answer.ordinal}:{pending.decision_id}"
    return None


def runner_argv(options_argv: Sequence[str], *, python: str = sys.executable) -> list[str]:
    """The child's command line: this interpreter, the runner module, the options."""
    return [python, "-m", "steerai.run", *options_argv]
