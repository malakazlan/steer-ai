"""Stage 2's expected outcomes (packet v2-ports-adapter, stage 2 plan rev 4.4 §1.2, [V2.63]):
computed, never hand-written, as stage 1's were (#369).

Each real-site task's acts are data (`stage2_acts.json`: the acted field's role and name as the
task's saved start-page read shows them, then its Enter or button). They are rated by stage 1's
own `rate` on that read and turned into an outcome by its own `outcome_of`, both unchanged.

The saved reads hold third-party page text, so they stay in a local directory outside the repo.
What is committed is the table: per task, the read's SHA-256 and its *deciding facts* (the acted
field's role and AriaRole, whether it sits under a Search landmark, the button's role and
landmark, and the rated risk), with the expected outcome. Never a whole-page hash: at run time
the harness re-reads the start page and compares these facts only (`site_changed`, [V2.63] Q4).

    uv run python -m steerai.loop2.stage2_expected --save-reads <dir>     # live, read-only
    uv run python -m steerai.loop2.stage2_expected --acts steerai/loop2/stage2_acts.json \\
        --reads <dir> --out <table.json>
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import re
import sys
import time
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Final, Literal, get_args

from steerai.loop2.decision import Key, Verb
from steerai.loop2.query import search_landmark_of
from steerai.loop2.stage1_expected import (
    Act,
    Expected,
    Outcome,
    ProofKind,
    outcome_of,
    rate,
    targets_in,
)
from steerai.orchestrator.contracts import RISKS, Risk
from steerai.probes.task_suite import H2, H3, H4, H5, H6, HELD_OUT, TASKS, Task
from steerai.world.effects import Observed
from steerai.world.elements import Element
from steerai.world.read_json import observed_from_json

ACTS: Final = Path(__file__).with_name("stage2_acts.json")
_ACT_FIELDS: Final = frozenset({"verb", "role", "name", "key", "option", "param"})
_ROW_FIELDS: Final = frozenset({"acts", "proof", "why"})
_PROOFS: Final = frozenset(get_args(ProofKind))
_VERBS: Final[dict[object, Verb]] = {v: v for v in get_args(Verb)}
_KEYS: Final[dict[object, Key]] = {k: k for k in get_args(Key)}
_RISKS: Final[dict[object, Risk]] = {r: r for r in RISKS}
SITE_SPACING_S: Final = 60.0
"""§2.3, [V2.63] Q4: the least time between two page loads of one host, controls included."""


@dataclass(frozen=True)
class SiteRow:
    """One real-site task's acts, the step 3 proof kind that reads its end state (None: none
    does), and one line saying why."""

    acts: tuple[Act, ...]
    proof: ProofKind | None
    why: str


@dataclass(frozen=True)
class DecidingFacts:
    """What a task's expected outcome was computed from, and nothing else."""

    field_role: str | None
    """The typed field's control type; None when the acts type into nothing."""
    aria_role: str | None
    """The typed field's AriaRole; None when unread or absent."""
    search_landmark: bool | None
    """The typed field sits under a Search landmark; None when the acts type into nothing."""
    button_role: str | None
    """The clicked button's control type; None when the acts click nothing (an Enter)."""
    button_search_landmark: bool | None
    risk: Risk
    unread: tuple[str, ...] = ()
    """The facts this read could not say (`aria_role`, `landmarks`): unread, never a value."""


@dataclass(frozen=True)
class TableRow:
    task_id: str
    read_sha256: str
    facts: DecidingFacts
    expected: Expected


START_EXPRESSION: Final = (
    "JSON.stringify({uri: document.documentURI, title: document.title, status: "
    "(performance.getEntriesByType('navigation')[0] || {}).responseStatus || 0})"
)
"""The start document over DevTools: its URI (Chrome's error page is `chrome-error:`), title, and
the main document's HTTP status (`responseStatus`; how it reads on real sites is unmeasured until
the save-reads slot records it)."""


@dataclass(frozen=True)
class StartDocument:
    status: int | None
    """The main document's HTTP status; None when unread (0, missing or not a number)."""
    error_page: bool
    title: str

    @property
    def loaded(self) -> bool:
        """A 2xx or 3xx main document that is not Chrome's own error page; unread is not."""
        ok = self.status is not None and 200 <= self.status < 400
        return ok and not self.error_page


def start_document(raw: str) -> StartDocument:
    """`START_EXPRESSION`'s JSON read back; anything unreadable is an unread status."""
    try:
        page = json.loads(raw)
    except ValueError:
        page = {}
    if not isinstance(page, dict):
        page = {}
    status = page.get("status")
    known = isinstance(status, int) and not isinstance(status, bool) and status > 0
    return StartDocument(
        status if known else None,
        str(page.get("uri", "")).startswith("chrome-error:"),
        str(page.get("title", "")),
    )


class Spacer:
    """§2.3: every load of a host waits until `spacing_s` after its last one, and is counted
    (kill-aware: an interrupt raised in the sleep ends the wait as itself)."""

    def __init__(
        self,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
        spacing_s: float = SITE_SPACING_S,
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        self._spacing_s = spacing_s
        self._last: dict[str, float] = {}
        self.loads: dict[str, int] = {}
        """Loads per host so far: the volume the summary reports."""

    def wait(self, host: str) -> None:
        last = self._last.get(host)
        if last is None:
            return
        left = last + self._spacing_s - self._clock()
        if left > 0:
            self._sleep(left)

    def loaded(self, host: str) -> None:
        self._last[host] = self._clock()
        self.loads[host] = self.loads.get(host, 0) + 1


def real_site_tasks() -> tuple[Task, ...]:
    """The pooled set's real-site tasks (dev, H1-H6), in set order, each once: the rows whose
    start is a web address. The sets are data; nothing here names a site."""
    seen: dict[str, Task] = {}
    for task in (*TASKS, *HELD_OUT, *H2, *H3, *H4, *H5, *H6):
        if task.start.startswith("https://"):
            seen.setdefault(task.task_id, task)
    return tuple(seen.values())


def deciding_facts(row: SiteRow, goal: str, read: Observed) -> DecidingFacts:
    """The deciding facts of `row`'s acts on `read`. A target missing from the read is a
    `ValueError` (`not_in_read`): the start page differs. A target with several copies is rated
    on each; when every choice gives the same deciding facts the rating does not depend on the
    copy Steer picks, and it stands. Otherwise it is `ambiguous_target`, naming the facts that
    differ (the coordinator's ruling, 2026-11-15)."""
    options = [[None] if act.verb == "press" else list(targets_in(act, read)) for act in row.acts]
    seen = {
        _facts_for(row, goal, read, dict(enumerate(choice)))
        for choice in itertools.product(*options)
    }
    if len(seen) > 1:
        differ = sorted(
            f.name for f in fields(DecidingFacts) if len({getattr(s, f.name) for s in seen}) > 1
        )
        raise ValueError(f"ambiguous_target: the copies differ on {differ}")
    return seen.pop()


def _facts_for(
    row: SiteRow, goal: str, read: Observed, picked: Mapping[int, Element | None]
) -> DecidingFacts:
    chosen = {i: e for i, e in picked.items() if e is not None}
    risk = rate(goal, row.acts, read, chosen=chosen)
    typed = next((i for i, a in enumerate(row.acts) if a.verb == "type"), None)
    clicked = next((i for i in reversed(range(len(row.acts))) if row.acts[i].verb == "click"), None)
    field = None if typed is None else chosen[typed]
    button = None if clicked is None else chosen[clicked]
    roles = read.aria_roles
    unread = []
    if field is not None and roles is None:
        unread.append("aria_role")
    if (field is not None or button is not None) and read.landmarks is None:
        unread.append("landmarks")
    return DecidingFacts(
        field_role=None if field is None else field.role,
        aria_role=None if field is None or roles is None else roles.get(field.runtime_id),
        search_landmark=None if field is None else _in_search(read, field.runtime_id),
        button_role=None if button is None else button.role,
        button_search_landmark=None if button is None else _in_search(read, button.runtime_id),
        risk=risk,
        unread=tuple(unread),
    )


def _in_search(read: Observed, runtime_id: tuple[int, ...]) -> bool:
    return search_landmark_of(read, runtime_id) is not None


SiteCheck = Literal["same", "changed", "unread"]


def site_check(committed: DecidingFacts, now: DecidingFacts) -> SiteCheck:
    """[V2.63] Q4: `changed` when any deciding fact differs, `same` when none does, and
    `unread` when this read could not say (an unread AriaRole or landmark map is never read as a
    change: #401 principal note)."""
    if now.unread:
        return "unread"
    return "same" if committed == now else "changed"


def table_row(task_id: str, row: SiteRow, goal: str, raw_read: bytes) -> TableRow:
    """One committed row from the saved read's bytes: its digest, its deciding facts and the
    expected outcome by stage 1's rule."""
    read = observed_from_json(raw_read.decode("utf-8"))
    facts = deciding_facts(row, goal, read)
    if facts.unread:
        raise ValueError(f"unread_facts: {task_id} {list(facts.unread)}")
    outcome, reason = outcome_of(facts.risk, row.proof)
    expected = Expected(task_id, outcome, reason, facts.risk, row.proof, row.why)
    return TableRow(task_id, hashlib.sha256(raw_read).hexdigest(), facts, expected)


def dump_table(rows: Sequence[TableRow], *, unrated: Mapping[str, str] | None = None) -> str:
    """The committed table as stable JSON, by task id; an unrated task is kept out of it as
    `{"unrated": reason}` (the coordinator's ruling, 2026-11-15)."""
    entries: dict[str, object] = {
        r.task_id: {
            "read_sha256": r.read_sha256,
            "facts": asdict(r.facts),
            "expected": asdict(r.expected),
        }
        for r in rows
    }
    entries |= {task_id: {"unrated": reason} for task_id, reason in (unrated or {}).items()}
    payload = {task_id: entries[task_id] for task_id in sorted(entries)}
    return json.dumps(payload, indent=2, sort_keys=False) + "\n"


UNRATED_REASONS: Final = frozenset({"target_after_opener", "pressed_unread"})
"""Tasks the lead rules on before they are rated (the coordinator, 2026-11-15): a target that
shows only after an opener, and a toggle whose pressed state the read does not show. They are
kept out of both tables and out of acceptance until then."""


def load_unrated(text: str) -> dict[str, str]:
    """The `{"unrated": reason}` entries of an acts file or a table, checked strictly."""
    raw = json.loads(text)
    if not isinstance(raw, dict):
        raise ValueError("unrated: not an object")
    return {
        str(task_id): _unrated(str(task_id), entry)
        for task_id, entry in raw.items()
        if isinstance(entry, dict) and "unrated" in entry
    }


def _unrated(task_id: str, entry: Mapping[str, object]) -> str:
    reason = entry.get("unrated")
    if set(entry) != {"unrated"} or reason not in UNRATED_REASONS:
        raise ValueError(f"unrated: {task_id} needs exactly one known reason")
    return str(reason)


def _rated(raw: Mapping[str, object]) -> dict[str, object]:
    """The entries that are not unrated markers (each marker checked)."""
    for task_id, entry in raw.items():
        if isinstance(entry, dict) and "unrated" in entry:
            _unrated(str(task_id), entry)
    return {str(k): v for k, v in raw.items() if not (isinstance(v, dict) and "unrated" in v)}


def load_table(text: str) -> dict[str, TableRow]:
    """The committed table, checked as strictly as the acts (#401 principal note): a
    `ValueError` naming `table` for anything else."""
    raw = json.loads(text)
    if not isinstance(raw, dict):
        raise ValueError("table: not an object")
    return {task_id: _table_row(task_id, entry) for task_id, entry in _rated(raw).items()}


_ENTRY_FIELDS: Final = frozenset({"read_sha256", "facts", "expected"})
_FACT_FIELDS: Final = frozenset(f.name for f in fields(DecidingFacts))
_EXPECTED_FIELDS: Final = frozenset(f.name for f in fields(Expected))
_OUTCOMES: Final = frozenset(get_args(Outcome))
_SHA256: Final = re.compile(r"[0-9a-f]{64}")


def _table_row(task_id: str, entry: object) -> TableRow:
    if not isinstance(entry, dict) or set(entry) != _ENTRY_FIELDS:
        raise ValueError(f"table: {task_id} needs exactly {sorted(_ENTRY_FIELDS)}")
    sha, facts, expected = entry["read_sha256"], entry["facts"], entry["expected"]
    if not isinstance(sha, str) or not _SHA256.fullmatch(sha):
        raise ValueError(f"table: {task_id} has no SHA-256")
    return TableRow(task_id, sha, _facts(task_id, facts), _expected(task_id, expected))


def _facts(task_id: str, raw: object) -> DecidingFacts:
    if not isinstance(raw, dict) or set(raw) != _FACT_FIELDS:
        raise ValueError(f"table: {task_id} needs exactly the deciding facts")
    texts = ("field_role", "aria_role", "button_role")
    flags = ("search_landmark", "button_search_landmark")
    if not all(raw[k] is None or isinstance(raw[k], str) for k in texts):
        raise ValueError(f"table: {task_id} has a role that is not text")
    if not all(raw[k] is None or isinstance(raw[k], bool) for k in flags):
        raise ValueError(f"table: {task_id} has a landmark that is not true or false")
    risk = _RISKS.get(raw["risk"])
    unread = raw["unread"]
    if risk is None:
        raise ValueError(f"table: {task_id} has an unknown risk")
    if not isinstance(unread, list) or unread:
        raise ValueError(f"table: {task_id} has unread facts")
    return DecidingFacts(
        raw["field_role"], raw["aria_role"], raw["search_landmark"],
        raw["button_role"], raw["button_search_landmark"], risk,
    )  # fmt: skip


def _expected(task_id: str, raw: object) -> Expected:
    if not isinstance(raw, dict) or set(raw) != _EXPECTED_FIELDS:
        raise ValueError(f"table: {task_id} needs exactly the expected outcome")
    outcome, risk, proof = raw["outcome"], raw["risk"], raw["proof"]
    if raw["task_id"] != task_id or outcome not in _OUTCOMES:
        raise ValueError(f"table: {task_id} has an unknown outcome")
    if (risk is not None and risk not in _RISKS) or (proof is not None and proof not in _PROOFS):
        raise ValueError(f"table: {task_id} has an unknown risk or proof")
    if not all(raw[k] is None or isinstance(raw[k], str) for k in ("reason", "why")):
        raise ValueError(f"table: {task_id} has a reason that is not text")
    return Expected(task_id, outcome, raw["reason"], risk, proof, raw["why"])


def load_acts(text: str) -> dict[str, SiteRow]:
    """The acts file: `{task_id: {"acts": [...], "proof": ..., "why": ...}}`, checked strictly
    (a `ValueError` naming `acts` for anything else)."""
    raw = json.loads(text)
    if not isinstance(raw, dict):
        raise ValueError("acts: not an object")
    return {task_id: _row(task_id, entry) for task_id, entry in _rated(raw).items()}


def _row(task_id: str, entry: object) -> SiteRow:
    if not isinstance(entry, dict) or set(entry) != _ROW_FIELDS:
        raise ValueError(f"acts: {task_id} needs exactly {sorted(_ROW_FIELDS)}")
    acts = entry["acts"]
    proof = entry["proof"]
    why = entry["why"]
    if not isinstance(acts, list) or not acts:
        raise ValueError(f"acts: {task_id} has no acts")
    if proof is not None and proof not in _PROOFS:
        raise ValueError(f"acts: {task_id} has an unknown proof kind")
    if not isinstance(why, str) or not why:
        raise ValueError(f"acts: {task_id} needs a why")
    return SiteRow(tuple(_act(task_id, a) for a in acts), proof, why)


def _act(task_id: str, raw: object) -> Act:
    if not isinstance(raw, dict) or not set(raw) <= _ACT_FIELDS:
        raise ValueError(f"acts: {task_id} has an act with unknown fields")
    verb = _VERBS.get(raw.get("verb"))
    key = None if raw.get("key") is None else _KEYS.get(raw.get("key"))
    if verb is None:
        raise ValueError(f"acts: {task_id} has an unknown verb")
    if key is None and raw.get("key") is not None:
        raise ValueError(f"acts: {task_id} has an unknown key")
    texts = {name: raw.get(name) for name in ("role", "name", "option", "param")}
    if not all(v is None or isinstance(v, str) for v in texts.values()):
        raise ValueError(f"acts: {task_id} has a field that is not text")
    return Act(
        verb,
        texts["role"] or "",
        texts["name"] or "",
        key=key,
        option=texts["option"],
        param=texts["param"],
    )


def table_from_reads(
    acts: Mapping[str, SiteRow], reads: Path, *, tasks: Sequence[Task] | None = None
) -> list[TableRow]:
    """Every row with acts, rated on its saved read `<reads>/<task_id>.json`; `tasks` are the
    group's tasks (the real-site ones by default; group 2a passes its own)."""
    goals = {t.task_id: t.goal for t in (real_site_tasks() if tasks is None else tasks)}
    unknown = sorted(set(acts) - set(goals))
    if unknown:
        raise ValueError(f"acts: not tasks of this group: {unknown}")
    return [
        table_row(task_id, row, goals[task_id], (reads / f"{task_id}.json").read_bytes())
        for task_id, row in acts.items()
    ]


def save_reads(out: Path, *, floor_mb: int) -> int:  # pragma: no cover - live, Windows only
    """The `--save-reads` pass: each real-site start page once, in a throwaway profile, read as
    the v2 loop's full read takes it, written to `<out>/<task_id>.json`. Read-only: no input.
    A start page shared by several tasks is loaded once."""
    import secrets  # noqa: PLC0415 - live only

    from steerai.probes.live_browser import live_page  # noqa: PLC0415 - Windows only
    from steerai.probes.memory_guard import EXIT_LOW_MEMORY, LowMemory  # noqa: PLC0415
    from steerai.probes.shadow_v2 import _ComFacts  # noqa: PLC0415 - COM failures as OSError
    from steerai.probes.task_suite import CHALLENGE_TITLES  # noqa: PLC0415 - live only
    from steerai.world.model import WorldModel  # noqa: PLC0415 - Windows only
    from steerai.world.observe2 import read_observed  # noqa: PLC0415 - Windows only
    from steerai.world.read_json import observed_to_json  # noqa: PLC0415

    out.mkdir(parents=True, exist_ok=True)
    by_start: dict[str, list[str]] = {}
    for task in real_site_tasks():
        by_start.setdefault(task.start, []).append(task.task_id)
    spacer = Spacer(time.monotonic, time.sleep)
    for start, task_ids in by_start.items():
        host = urllib.parse.urlsplit(start).netloc
        spacer.wait(host)
        spacer.loaded(host)
        try:
            with live_page(start, floor_mb=floor_mb, settle_s=6.0) as page:
                reply = page.cdp.call(
                    "Runtime.evaluate", {"expression": START_EXPRESSION, "returnByValue": True}
                )
                document = start_document(str(reply.get("result", {}).get("value", "")))
                snapshot = WorldModel(page.client).observe(page.hwnd, cap_s=5.0)
                read = read_observed(
                    snapshot,
                    _ComFacts(page.client),
                    key=secrets.token_bytes(32),
                    windows=lambda: (page.hwnd,),
                )
        except LowMemory as refused:
            sys.stdout.write(json.dumps({"tasks": task_ids, "refused": str(refused)}) + "\n")
            return EXIT_LOW_MEMORY
        except (OSError, TimeoutError, RuntimeError) as exc:
            sys.stdout.write(json.dumps({"tasks": task_ids, "error": type(exc).__name__}) + "\n")
            continue
        text = (observed_to_json(read) + "\n").encode("utf-8")
        for task_id in task_ids:
            (out / f"{task_id}.json").write_bytes(text)
        row = {"tasks": task_ids, "elements": len(snapshot.elements)}
        # stage 2 §2.2 step 3, unmeasured until here: the status reading on each real site, and
        # whether the page is a known wall (a fixed title token, never page text)
        row |= {"status": document.status, "error_page": document.error_page,
                "wall": document.title in CHALLENGE_TITLES}  # fmt: skip
        sys.stdout.write(json.dumps(row) + "\n")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage 2's expected outcomes.")
    parser.add_argument("--save-reads", type=Path, help="live: save each start page's read here")
    parser.add_argument("--min-free-mb", type=int, default=4096)
    parser.add_argument("--acts", type=Path, default=ACTS)
    parser.add_argument("--reads", type=Path, help="the saved reads' local directory")
    parser.add_argument("--out", type=Path, help="write the table here")
    args = parser.parse_args(argv)
    if args.save_reads is not None:  # pragma: no cover - live
        return save_reads(args.save_reads, floor_mb=args.min_free_mb)
    if args.reads is None:
        parser.error("--reads is required to compute the table")
    rows = table_from_reads(load_acts(args.acts.read_text(encoding="utf-8")), args.reads)
    for r in rows:
        e = r.expected
        sys.stdout.write(f"{r.task_id:<6} {e.risk:<3} {e.outcome:<11} {e.reason or '-'}\n")
    if args.out is not None:
        args.out.write_bytes(dump_table(rows).encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
