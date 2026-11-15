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
import dataclasses
import datetime
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
from typing import Any, Final, Literal, get_args

from steerai.loop2.decision import Key, Verb
from steerai.loop2.form import beside
from steerai.loop2.query import search_landmark_of
from steerai.loop2.stage1_expected import (
    Act,
    Expected,
    Outcome,
    ProofKind,
    outcome_of,
    rate,
    target_in,
    targets_in,
)
from steerai.orchestrator.contracts import RISKS, Risk
from steerai.probes.task_suite import H2, H3, H4, H5, H6, HELD_OUT, TASKS, Task
from steerai.world.effects import Observed
from steerai.world.elements import Element
from steerai.world.model import normalise_name
from steerai.world.read_json import observed_from_json

ACTS: Final = Path(__file__).with_name("stage2_acts.json")
TABLE: Final = Path(__file__).with_name("stage2_expected.json")
"""The committed 2b table: each read's SHA-256, its deciding facts and the expected outcome."""
TABLE_SHA256: Final = "89ceab94e04b2b515c814e61ecc57812d90d52af19daf5cd594494e5424d68e9"
"""#424 (2): the committed 2b table's pin, as 2a's: a run takes only this table."""
_ACT_FIELDS: Final = frozenset({"verb", "role", "name", "key", "option", "param"})
_ROW_FIELDS: Final = frozenset({"acts", "proof", "why"})
_PROOFS: Final = frozenset(get_args(ProofKind))
_VERBS: Final[dict[object, Verb]] = {v: v for v in get_args(Verb)}
_KEYS: Final[dict[object, Key]] = {k: k for k in get_args(Key)}
_RISKS: Final[dict[object, Risk]] = {r: r for r in RISKS}
SITE_SPACING_S: Final = 60.0
"""§2.3, [V2.63] Q4: the least time between two page loads of one host, controls included."""
DAILY_CAP: Final = 12
"""[V2.65] (3): the most loads of one host in one day at the spacing, a hard cap (#419): an
attempt starts only with its whole budget left today."""
ATTEMPT_LOADS: Final = 2
"""#419: the loads one attempt can make on its host, before its control: the run's own load and a
possible wall confirmation."""
DAY_WAIT_CHUNK_S: Final = 3600.0
"""#413 blocker 1: the wait for the day to turn sleeps at most this long at a time."""
DAY_WAIT_LIMIT_S: Final = 26 * 3600.0
"""#413 blocker 1: a day that has not turned after this long is refused (fail closed)."""


class DayDidNotTurn(RuntimeError):  # noqa: N818 - a refusal, named for its reason
    """Every pending host was at its cap and the calendar day did not turn in the bound."""

    def __init__(self) -> None:
        super().__init__("day_did_not_turn")


def calendar_day() -> int:
    """Today on the local calendar, as an ordinal: the production day source (#413 blocker 1)."""
    return datetime.date.today().toordinal()  # noqa: DTZ011 - the local day the cap counts


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
    """§2.3: every load of a host waits until `spacing_s` after its last one, and is counted, in
    all and per day ([V2.65] (3): at most `cap` a host a day). Kill-aware: an interrupt raised in
    a sleep ends the wait as itself."""

    def __init__(
        self,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
        spacing_s: float = SITE_SPACING_S,
        *,
        day: Callable[[], int] = calendar_day,
        cap: int = DAILY_CAP,
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        self._spacing_s = spacing_s
        self._last: dict[str, float] = {}
        self._today: dict[tuple[int, str], int] = {}
        self._cap = cap
        self.day = day
        """The calendar day now (an ordinal): the cap and a control's repeat read it."""
        self.loads: dict[str, int] = {}
        """Loads per host so far: the volume the summary reports."""

    def at_cap(self, host: str) -> bool:
        return self.remaining(host) <= 0

    def remaining(self, host: str) -> int:
        """Loads of `host` left today under the cap."""
        return self._cap - self._today.get((self.day(), host), 0)

    def wait_next_day(self) -> None:
        """Every pending host is at its cap: wait, kill-aware, until the day turns, in sleeps of
        at most `DAY_WAIT_CHUNK_S`; a day that has not turned after `DAY_WAIT_LIMIT_S` is refused
        (`DayDidNotTurn`), never waited on forever."""
        today = self.day()
        waited = 0.0
        while self.day() == today:
            if waited >= DAY_WAIT_LIMIT_S:
                raise DayDidNotTurn
            self._sleep(DAY_WAIT_CHUNK_S)
            waited += DAY_WAIT_CHUNK_S

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
        key = (self.day(), host)
        self._today[key] = self._today.get(key, 0) + 1


# ---- [V2.65] (1) option (b): a target revealed by one scripted click on its opener ---------------

OPENERS_FILE: Final = Path(__file__).with_name("stage2_openers.json")
OPENER_VERBS: Final = frozenset({"click", "expand"})
AX_ROLES: Final[Mapping[str, str]] = {
    "Button": "button",
    "TreeItem": "treeitem",
    "MenuItem": "menuitem",
    "Hyperlink": "link",
    "TabItem": "tab",
}
"""A UIA control type as Chrome's accessibility tree names the same element's role."""
REVEAL_LIMIT_S: Final = 12.0
"""How long a revealed state may take to settle after the opener's click (unmeasured)."""
REVEAL_POLL_S: Final = 0.5
_CLICK: Final = "function () { this.click(); }"


class OpenerError(RuntimeError):
    """The opener was not found exactly once: it is never clicked, and the task stays unrated."""


def load_openers(text: str) -> dict[str, Act]:
    """`{task_id: {"verb", "role", "name"}}`: one named click or expand each, nothing else; a
    `ValueError` naming `openers` for anything else."""
    raw = json.loads(text)
    if not isinstance(raw, dict):
        raise ValueError("openers: not an object")
    openers: dict[str, Act] = {}
    for task_id, entry in raw.items():
        if not isinstance(entry, dict) or set(entry) != {"verb", "role", "name"}:
            raise ValueError(f"openers: {task_id} needs exactly verb, role and name")
        verb, role, name = entry["verb"], entry["role"], entry["name"]
        if (
            verb not in OPENER_VERBS
            or role not in AX_ROLES
            or not isinstance(name, str)
            or not name
        ):
            raise ValueError(f"openers: {task_id} is not one named click or expand")
        openers[str(task_id)] = Act(_VERBS[verb], role, name)
    return openers


OPENERS: Final[Mapping[str, Act]] = load_openers(OPENERS_FILE.read_text(encoding="utf-8"))


Cdp = Callable[..., Mapping[str, Any]]
"""A DevTools call: `(method, params) -> result` (Any: JSON)."""


def click_opener(cdp: Cdp, opener: Act) -> None:
    """The harness's own scripted click (never Steer's input): the one element of the opener's
    role and name in the page's accessibility tree, clicked through its own `click()`. Not found
    exactly once is an `OpenerError`; nothing is typed, submitted or answered."""
    wanted = normalise_name(opener.name)
    role = AX_ROLES[opener.role]
    nodes = [
        n for n in cdp("Accessibility.getFullAXTree", {}).get("nodes", []) if isinstance(n, Mapping)
    ]
    matches = [
        n
        for n in nodes
        if not n.get("ignored")
        and n.get("role", {}).get("value") == role
        and normalise_name(str(n.get("name", {}).get("value", ""))) == wanted
        and isinstance(n.get("backendDOMNodeId"), int)
    ]
    found = [n["backendDOMNodeId"] for n in _outermost_ax(matches, nodes, role)]
    if not found:
        raise OpenerError("opener_not_found")
    if len(found) > 1:
        raise OpenerError("opener_ambiguous")
    target = cdp("DOM.resolveNode", {"backendNodeId": found[0]})
    object_id = target.get("object", {}).get("objectId")
    if not isinstance(object_id, str):
        raise OpenerError("opener_not_resolved")
    cdp("Runtime.callFunctionOn", {"objectId": object_id, "functionDeclaration": _CLICK})


def check_table(text: str) -> None:
    """#424 (2): the run's 2b table is the committed one, byte for byte."""
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != TABLE_SHA256:
        raise ValueError("table: not the committed 2b table")


def fresh_opened(out: Path, task_id: str) -> Path:
    """#424 (1): an opener attempt's target file, any earlier one removed first, so a later
    unstable or failed reveal never leaves a stale opened read to be rated."""
    path = out / f"{task_id}.opened.json"  # #428 (1): never the start read's name
    path.unlink(missing_ok=True)
    return path


def _outermost_ax(
    matches: Sequence[Mapping[str, Any]], nodes: Sequence[Mapping[str, Any]], role: str
) -> list[Mapping[str, Any]]:
    """#424 (3): the start check's counting rule (`outermost`) on the accessibility tree: a
    match inside another match is the same element and folds into it, except a tree item (a
    folder inside a same-named folder is another item)."""
    if role == "treeitem":
        return list(matches)
    parent = {str(n.get("nodeId")): n.get("parentId") for n in nodes}
    ids = {str(m.get("nodeId")) for m in matches}

    def inside(node: Mapping[str, Any]) -> bool:
        seen: set[str] = set()
        up = parent.get(str(node.get("nodeId")))
        while up is not None and str(up) not in seen:
            if str(up) in ids:
                return True
            seen.add(str(up))
            up = parent.get(str(up))
        return False

    return [m for m in matches if not inside(m)]


def reveal_targets(row: SiteRow) -> tuple[Act, ...]:
    """#424 (4): the acts after the opener that name a target: the reveal must show them."""
    return tuple(a for a in row.acts[1:] if a.verb != "press" and a.role)


def opened_read_path(reads: Path, task_id: str) -> Path:
    """A task behind an opener is rated on its opened read; every other on its start read."""
    return reads / (f"{task_id}.opened.json" if task_id in OPENERS else f"{task_id}.json")


def opener_start_facts(
    opener: Act, start: Observed, committed: DecidingFacts
) -> DecidingFacts | None:
    """The run's start check for a task behind an opener: its target is not on the start page,
    so the start page must show the opener exactly once, and the committed facts (read on the
    opened page) stand; None when it does not (the start page differs)."""
    try:
        target_in(opener, start)
    except ValueError:
        return None
    return committed


@dataclass(frozen=True)
class Revealed:
    read: Observed
    new_roles: dict[str, int]
    """How many new elements of each role the opener revealed (closed values, no names)."""


def _in_page(read: Observed) -> list[Element]:
    """The elements under the page's Document: the browser's own frame (a tab's title, a notice
    button) changes on its own and is never a reveal (the slot's first opener pass)."""
    elements = read.snapshot.elements
    children: dict[int, list[Element]] = {}
    for e in elements:
        if e.parent_id is not None:
            children.setdefault(e.parent_id, []).append(e)
    found: list[Element] = []
    stack = [e for e in elements if e.role == "Document"]
    while stack:
        e = stack.pop()
        found.append(e)
        stack.extend(children.get(e.id, []))
    return found


def wait_revealed(  # noqa: PLR0913 - the reads, then keyword-only knobs
    read: Callable[[], Observed],
    before: Observed,
    *,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    limit_s: float = REVEAL_LIMIT_S,
    targets: Sequence[Act] = (),
) -> Revealed | None:
    """The revealed state once it shows the same new elements in the page (by role and name,
    against the read before the click) on two reads in a row within `limit_s`; None when it does
    not: the reveal is not deterministic and the task stays unrated."""
    seen_before = {(e.role, normalise_name(e.name)) for e in _in_page(before)}
    deadline = clock() + limit_s
    last: tuple[tuple[str, str], ...] | None = None
    while clock() < deadline:
        now = read()
        new = tuple(
            sorted(
                (e.role, normalise_name(e.name))
                for e in _in_page(now)
                if (e.role, normalise_name(e.name)) not in seen_before
            )
        )
        wanted = {(a.role, normalise_name(a.name)) for a in targets if a.role}
        if new and new == last and wanted <= set(new):  # #424 (4): the row's own target
            roles: dict[str, int] = {}
            for role, _name in new:
                roles[role] = roles.get(role, 0) + 1
            return Revealed(now, roles)
        last = new
        sleep(REVEAL_POLL_S)
    return None


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
    choices = [dict(enumerate(choice)) for choice in itertools.product(*options)]
    in_scope = [c for c in choices if _paired(row, read, c)]
    seen = {_facts_for(row, goal, read, choice) for choice in (in_scope or choices)}
    if len(seen) > 1:
        differ = sorted(
            f.name for f in fields(DecidingFacts) if len({getattr(s, f.name) for s in seen}) > 1
        )
        raise ValueError(f"ambiguous_target: the copies differ on {differ}")
    return seen.pop()


def _paired(row: SiteRow, read: Observed, picked: Mapping[int, Element | None]) -> bool:
    """A click pairs with the copy of the field typed before it only when it is in that field's
    [V2.22] submit scope (`form.beside`, the gate's own): typing into one copy of a form and
    clicking another copy's button is no submit of the text. An unknown relation pairs (the gate
    reads it as a submit too). With no click after a type, every choice pairs."""
    typed: Element | None = None
    for index, act in enumerate(row.acts):
        element = picked.get(index)
        if act.verb == "type":
            typed = element
        elif (
            act.verb == "click"
            and typed is not None
            and element is not None
            and beside(read, typed.runtime_id, element) is False
        ):
            return False
    return True


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


def opener_table_row(
    task_id: str, row: SiteRow, goal: str, raw_start: bytes, raw_opened: bytes
) -> TableRow:
    """A row behind an opener ([V2.65] (1)): its first act is the opener, rated on the start
    read (the opener may vanish once clicked); the rest are rated on the opened read, and the
    row's risk is the higher of the two. The opened read's digest is committed."""
    opener = OPENERS.get(task_id)
    if opener is None or not row.acts or row.acts[0] != opener:
        raise ValueError(f"opener: {task_id}'s acts must start with its opener")
    start = observed_from_json(raw_start.decode("utf-8"))
    opened = observed_from_json(raw_opened.decode("utf-8"))
    opener_risk = rate(goal, (opener,), start)
    rest = deciding_facts(SiteRow(row.acts[1:], row.proof, row.why), goal, opened)
    if rest.unread:
        raise ValueError(f"unread_facts: {task_id} {list(rest.unread)}")
    risk = max(opener_risk, rest.risk, key=RISKS.index)
    facts = dataclasses.replace(rest, risk=risk)
    outcome, reason = outcome_of(risk, row.proof)
    expected = Expected(task_id, outcome, reason, risk, row.proof, row.why)
    return TableRow(task_id, hashlib.sha256(raw_opened).hexdigest(), facts, expected)


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


UNRATED_REASONS: Final = frozenset({"target_after_opener", "pressed_unread", "copies_disagree"})
"""Tasks the lead rules on before they are rated (the coordinator, 2026-11-15): a target that
shows only after an opener, a toggle whose pressed state the read does not show, and a target
whose copies disagree on a deciding fact (no guess at which copy Steer acts on). They are kept
out of both tables and out of acceptance until then."""


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
        opener_table_row(
            task_id,
            row,
            goals[task_id],
            (reads / f"{task_id}.json").read_bytes(),
            opened_read_path(reads, task_id).read_bytes(),
        )
        if task_id in OPENERS
        else table_row(task_id, row, goals[task_id], (reads / f"{task_id}.json").read_bytes())
        for task_id, row in acts.items()
    ]


def full_read(client: Any, hwnd: int) -> Observed:  # pragma: no cover - live; Any: UiaClient
    """The window's full read as the v2 loop takes it (read-only)."""
    import secrets  # noqa: PLC0415 - live only

    from steerai.probes.shadow_v2 import _ComFacts  # noqa: PLC0415 - COM failures as OSError
    from steerai.world.model import WorldModel  # noqa: PLC0415 - Windows only
    from steerai.world.observe2 import read_observed  # noqa: PLC0415 - Windows only

    return read_observed(
        WorldModel(client).observe(hwnd, cap_s=5.0),
        _ComFacts(client),
        key=secrets.token_bytes(32),
        windows=lambda: (hwnd,),
    )


def save_opened(out: Path, *, floor_mb: int) -> int:  # pragma: no cover - live, Windows only
    """[V2.65] (1) (b): each real-site task behind an opener, once, in a fresh throwaway
    profile, spaced and capped per host: the start page read, the opener clicked once by the
    harness, and the revealed state read when it settles (`<task_id>.opened.json`). Nothing is
    typed or submitted; a reveal that does not settle leaves the task unrated."""
    from steerai.probes.live_browser import live_page  # noqa: PLC0415 - Windows only
    from steerai.probes.memory_guard import EXIT_LOW_MEMORY, LowMemory  # noqa: PLC0415
    from steerai.world.read_json import observed_to_json  # noqa: PLC0415

    out.mkdir(parents=True, exist_ok=True)
    spacer = Spacer(time.monotonic, time.sleep)
    acted = load_acts(ACTS.read_text(encoding="utf-8")) if ACTS.exists() else {}
    for task in real_site_tasks():
        opener = OPENERS.get(task.task_id)
        if opener is None:
            continue
        host = urllib.parse.urlsplit(task.start).netloc
        spacer.wait(host)
        spacer.loaded(host)
        row: dict[str, object] = {"task": task.task_id}
        path = fresh_opened(out, task.task_id)
        try:
            with live_page(task.start, floor_mb=floor_mb, settle_s=6.0) as page:
                reply = page.cdp.call(
                    "Runtime.evaluate", {"expression": START_EXPRESSION, "returnByValue": True}
                )
                document = start_document(str(reply.get("result", {}).get("value", "")))
                row |= {"status": document.status, "error_page": document.error_page}
                before = full_read(page.client, page.hwnd)
                click_opener(page.cdp.call, opener)
                revealed = wait_revealed(
                    lambda: full_read(page.client, page.hwnd),
                    before,
                    clock=time.monotonic,
                    sleep=time.sleep,
                    targets=()
                    if acted.get(task.task_id) is None
                    else reveal_targets(acted[task.task_id]),
                )
        except LowMemory as refused:
            sys.stdout.write(json.dumps({**row, "refused": str(refused)}) + "\n")
            return EXIT_LOW_MEMORY
        except OpenerError as exc:
            sys.stdout.write(json.dumps({**row, "opener": str(exc)}) + "\n")
            continue
        except (OSError, TimeoutError, RuntimeError) as exc:
            sys.stdout.write(json.dumps({**row, "error": type(exc).__name__}) + "\n")
            continue
        if revealed is None:
            sys.stdout.write(json.dumps({**row, "revealed": False}) + "\n")
            continue
        path.write_bytes((observed_to_json(revealed.read) + "\n").encode("utf-8"))
        sys.stdout.write(json.dumps({**row, "revealed": True, "new": revealed.new_roles}) + "\n")
    return 0


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
    parser.add_argument("--save-opened", type=Path, help="live: the reads behind each opener")
    parser.add_argument("--min-free-mb", type=int, default=4096)
    parser.add_argument("--acts", type=Path, default=ACTS)
    parser.add_argument("--reads", type=Path, help="the saved reads' local directory")
    parser.add_argument("--out", type=Path, help="write the table here")
    args = parser.parse_args(argv)
    if args.save_reads is not None:  # pragma: no cover - live
        return save_reads(args.save_reads, floor_mb=args.min_free_mb)
    if args.save_opened is not None:  # pragma: no cover - live
        return save_opened(args.save_opened, floor_mb=args.min_free_mb)
    if args.reads is None:
        parser.error("--reads is required to compute the table")
    acts_text = args.acts.read_text(encoding="utf-8")
    rows = table_from_reads(load_acts(acts_text), args.reads)
    for r in rows:
        e = r.expected
        sys.stdout.write(f"{r.task_id:<6} {e.risk:<3} {e.outcome:<11} {e.reason or '-'}\n")
    if args.out is not None:
        table = dump_table(rows, unrated=load_unrated(acts_text))
        args.out.write_bytes(table.encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
