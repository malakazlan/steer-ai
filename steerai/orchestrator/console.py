"""The console confirmer: the ask surface until the overlay exists (design 5.1 [v1.3]).

The console is the person's own screen, so it shows what a trace never carries: the target's
name and automation id, the text about to be sent, and a warning when an earlier attempt may
already have landed. Everything is rendered to one string first, with every non-printable
character escaped (a plan file cannot rewrite the console, RN-05), then written with a
backslash fallback when the stream cannot encode it (RN-08). Exactly one line is read per ask;
only the exact tokens count, a bare `yes` only when a person is at the console (RN-09/24/42).
"""

from __future__ import annotations

import json
import sys
from typing import TextIO

from steerai.orchestrator.gate import Ask

MAX_ANSWER_CHARS = 4096
"""An answer longer than this is not an answer (RN-42)."""
_PREFIX = "  "


def escape_text(value: str) -> str:
    """Every non-printable character as an escape: what the console shows can never
    rewrite it (RN-05)."""
    return _show(value)


def _show(value: str) -> str:
    out: list[str] = []
    for ch in value:
        if ch.isprintable():
            out.append(ch)
        elif ord(ch) < 0x100:
            out.append(f"\\x{ord(ch):02x}")
        elif ord(ch) <= 0xFFFF:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(f"\\U{ord(ch):08x}")
    return "".join(out)


def render_ask(ask: Ask) -> str:
    """The ask as printable lines; the first line is parseable: `ASK <id> <verb> <risk> <role>`."""
    lines = [f"ASK {_show(ask.decision_id)} {_show(ask.verb)} {ask.risk} {_show(ask.target_role)}"]
    target = _show(ask.target_name) or "(unnamed)"
    if ask.automation_id:
        target += f"  [automation id {_show(ask.automation_id)}]"
    lines.append(f"{_PREFIX}target: {target}")
    text = ask.params.get("text")
    if text is not None:
        lines.append(f"{_PREFIX}text: {_show(text)}")
    for key in sorted(ask.params):
        if key != "text":
            lines.append(f"{_PREFIX}{_show(key)}: {_show(ask.params[key])}")
    lines.append(f"{_PREFIX}expect: {_show(json.dumps(dict(ask.expect), sort_keys=True))}")
    if ask.previous_attempt is not None:
        lines.append(
            f"{_PREFIX}WARNING: an earlier attempt of this action may already have landed "
            f"(phase: {_show(ask.previous_attempt)}); check the screen before answering."
        )
    lines.append(
        f"{_PREFIX}the target window holds focus until you answer here; "
        f"type yes:{_show(ask.decision_id)} to approve, no to refuse"
    )
    return "\n".join(lines) + "\n"


class ConsoleConfirmer:
    """Asks on `stderr`, reads one line from `stdin`; never approves on anything but the token."""

    def __init__(
        self,
        stdin: TextIO | None = None,
        stderr: TextIO | None = None,
        *,
        interactive: bool | None = None,
    ) -> None:
        self._stdin = sys.stdin if stdin is None else stdin
        self._stderr = sys.stderr if stderr is None else stderr
        if interactive is None:
            try:
                interactive = bool(self._stdin.isatty())
            except (AttributeError, ValueError, OSError):
                interactive = False
        self._interactive = interactive
        self._warned = False

    def ask(self, ask: Ask) -> str:
        try:
            text = render_ask(ask)
        except (TypeError, ValueError):
            return "unanswered"
        if not self._write(text):
            return "unanswered"
        line = self._read()
        if line is None:
            return "unanswered"
        return self._classify(line.rstrip("\r\n").strip(), ask.decision_id)

    def _classify(self, answer: str, decision_id: str) -> str:
        bound_yes = f"yes:{decision_id}"
        if answer in (bound_yes, "no", f"no:{decision_id}"):
            return answer
        if answer == "yes" and self._interactive:
            return "yes"
        if answer == "yes":
            self._write(
                f"{_PREFIX}stdin is not interactive: only the bound token {bound_yes} counts\n"
            )
        return "unanswered"

    def _write(self, text: str) -> bool:
        try:
            try:
                self._stderr.write(text)
            except UnicodeEncodeError:
                encoding = getattr(self._stderr, "encoding", None) or "ascii"
                self._stderr.write(text.encode(encoding, "backslashreplace").decode(encoding))
            self._stderr.flush()
        except (OSError, ValueError, UnicodeError):
            return False
        return True

    def _read(self) -> str | None:
        try:
            line = self._stdin.readline(MAX_ANSWER_CHARS + 1)
        except KeyboardInterrupt:
            return None
        except (OSError, ValueError, UnicodeError):
            return None
        if not line or len(line) > MAX_ANSWER_CHARS:
            return None
        return line
