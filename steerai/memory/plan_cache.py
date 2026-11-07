"""The verified plan cache (design 5.12 skill level, Phase 4 step 1 [v2.33]): a request the agent
already completed and verified runs again without a planner call, through the same gate,
preview and verifier.

An entry holds the plan document's bytes, the goal-match verdict tokens judged on exactly those
bytes, and its score. Entries are encrypted to the user with DPAPI under a purpose entropy, and
each file is named by an HMAC under a DPAPI-protected cache secret, so neither a file name nor a
file reveals a goal (gate item 5). Anything unreadable, forged or of another key is a miss and is
deleted, never a crash. The key is the request's goal exactly (whitespace folded, NFKC, case
kept), the start window's app kind, the planner prompt version and the matcher's model and
prompt version (gate item 1): any change is a miss.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from steerai.orchestrator.contracts import Plan, StepResult, verbatim_in
from steerai.world.model import normalise_name

State = Literal["probation", "trusted", "evicted"]
Outcome = Literal["success", "fail", "neutral"]
TRUSTED_AFTER = 3
"""Verified successes before an entry is trusted (5.12 starting policy)."""
EVICTED_AFTER = 2
"""Consecutive plan-caused failures before an entry is evicted (5.12 starting policy)."""
ENTRY_ENTROPY = b"steer-plan-cache-entry-v1"
SECRET_ENTROPY = b"steer-plan-cache-secret-v1"
_VERDICTS = frozenset({"serves", "unrelated", "contrary", "unknown"})
_STATES = frozenset({"probation", "trusted"})
_HOST_KINDS = frozenset({"applicationframehost.exe"})
"""One exe hosts many apps: never a cache kind (hunter PC-17)."""
_BROWSER_KINDS = frozenset({"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe"})
_CONTENT_ROLES = frozenset({"ListItem", "Hyperlink", "DataItem", "TreeItem", "Text"})
_PLAN_CAUSED = frozenset({"verify_failed", "verify_timeout", "element_not_found"})


@dataclass(frozen=True, slots=True)
class CacheKey:
    goal: str = field(repr=False)
    app_kind: str
    prompt_version: str
    matcher: str
    """The goal-match model and prompt version the verdicts were judged with."""

    @classmethod
    def of(cls, goal: str, *, app_kind: str, prompt_version: str, matcher: str) -> CacheKey:
        return cls(normalise_name(goal), app_kind.casefold(), prompt_version, matcher)

    def text(self) -> str:
        return json.dumps(
            [self.goal, self.app_kind, self.prompt_version, self.matcher], ensure_ascii=False
        )


@dataclass(frozen=True, slots=True)
class Entry:
    key: CacheKey
    plan: bytes = field(repr=False)
    verdicts: Mapping[str, str]
    state: State = "probation"
    uses: int = 1
    fails: int = 0
    consecutive_fails: int = 0
    updated: float = 0.0

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.plan).hexdigest()


@dataclass(frozen=True, slots=True)
class Lookup:
    entry: Entry | None
    event: Literal["hit", "miss", "corrupt"]


Protect = Callable[..., bytes]


@dataclass
class PlanCache:
    root: Path
    protect: Protect
    unprotect: Protect
    clock: Callable[[], float] = time.time
    _secret: bytes | None = None

    def get(self, key: CacheKey) -> Lookup:
        path = self._path(key)
        if not path.exists():
            return Lookup(None, "miss")
        try:
            entry = self._decode(path.read_bytes())
        except (OSError, ValueError, KeyError, TypeError):
            path.unlink(missing_ok=True)
            return Lookup(None, "corrupt")
        if entry.key != key:  # a file copied over another key's name (PC-25)
            path.unlink(missing_ok=True)
            return Lookup(None, "corrupt")
        return Lookup(entry, "hit")

    def put(self, key: CacheKey, plan: bytes, verdicts: Mapping[str, str]) -> Entry:
        """A new entry from one verified run: probation, one use."""
        entry = Entry(key, plan, dict(verdicts), updated=self.clock())
        self._write(entry)
        return entry

    def score(self, entry: Entry, outcome: Outcome) -> State:
        """A hit's result: a success adds a use (trusted at three), a plan-caused failure adds
        a consecutive fail (evicted, and deleted, at two); neutral changes nothing."""
        if outcome == "neutral":
            return entry.state
        if outcome == "success":
            uses = entry.uses + 1
            state: State = "trusted" if uses >= TRUSTED_AFTER else entry.state
            self._write(
                replace(entry, uses=uses, consecutive_fails=0, state=state, updated=self.clock())
            )
            return state
        consecutive = entry.consecutive_fails + 1
        if consecutive >= EVICTED_AFTER:
            self._path(entry.key).unlink(missing_ok=True)
            return "evicted"
        self._write(
            replace(
                entry, fails=entry.fails + 1, consecutive_fails=consecutive, updated=self.clock()
            )
        )
        return entry.state

    # ---- files -------------------------------------------------------------------------------

    def _path(self, key: CacheKey) -> Path:
        name = hmac.new(self._key_secret(), key.text().encode(), hashlib.sha256).hexdigest()
        return self.root / f"{name[:40]}.entry"

    def _key_secret(self) -> bytes:
        if self._secret is not None:
            return self._secret
        path = self.root / "secret.bin"
        self.root.mkdir(parents=True, exist_ok=True)
        if path.exists():
            self._secret = self.unprotect(path.read_bytes(), entropy=SECRET_ENTROPY)
        else:
            fresh = secrets.token_bytes(32)
            self._atomic(path, self.protect(fresh, entropy=SECRET_ENTROPY))
            self._secret = fresh
        return self._secret

    def _write(self, entry: Entry) -> None:
        payload = {
            "key": [
                entry.key.goal,
                entry.key.app_kind,
                entry.key.prompt_version,
                entry.key.matcher,
            ],
            "plan": base64.b64encode(entry.plan).decode("ascii"),
            "verdicts": dict(entry.verdicts),
            "state": entry.state,
            "uses": entry.uses,
            "fails": entry.fails,
            "consecutive_fails": entry.consecutive_fails,
            "updated": entry.updated,
        }
        blob = self.protect(json.dumps(payload).encode(), entropy=ENTRY_ENTROPY)
        self._atomic(self._path(entry.key), blob)

    @staticmethod
    def _atomic(path: Path, blob: bytes) -> None:
        temporary = path.with_name(f"{path.name}.{secrets.token_hex(6)}.tmp")
        temporary.write_bytes(blob)
        temporary.replace(path)

    def _decode(self, blob: bytes) -> Entry:
        data = json.loads(self.unprotect(blob, entropy=ENTRY_ENTROPY).decode())
        goal, app_kind, prompt_version, matcher = (str(v) for v in data["key"])
        verdicts = data["verdicts"]
        if not isinstance(verdicts, dict) or not all(
            isinstance(k, str) and v in _VERDICTS for k, v in verdicts.items()
        ):
            raise ValueError("verdicts")
        state = data["state"]
        counts = [data["uses"], data["fails"], data["consecutive_fails"]]
        if state not in _STATES or not all(isinstance(c, int) and c >= 0 for c in counts):
            raise ValueError("state or counts")
        return Entry(
            CacheKey(goal, app_kind, prompt_version, matcher),
            base64.b64decode(data["plan"], validate=True),
            verdicts,
            state=state,
            uses=counts[0],
            fails=counts[1],
            consecutive_fails=counts[2],
            updated=float(data["updated"]),
        )


# ---- the rules (pure) ----------------------------------------------------------------------


def refusal(plan: Plan, goal: str, app_kind: str | None) -> str | None:
    """Why this plan may not be cached or replayed for this goal, or None. A browser start is
    cached only when the plan opens its own page (PC-04); a step on screen content the goal
    does not name is time- or screen-dependent ("the latest video", PC-03)."""
    if not app_kind or app_kind.casefold() in _HOST_KINDS:
        return "app_kind"
    if normalise_name(plan.goal) != normalise_name(goal):
        return "goal"
    if app_kind.casefold() in _BROWSER_KINDS and (not plan.steps or plan.steps[0].verb != "open"):
        return "browser_site"
    for step in plan.steps:
        target = step.target
        if target is None or target.role not in _CONTENT_ROLES:
            continue
        if not verbatim_in(target.name, goal):
            return "screen_content"
    return None


def outcome_of(plan: Plan, results: Sequence[StepResult], *, replanned: bool) -> Outcome:
    """A run's verdict for the cache (gate items 2-3): a success only when every step of the
    plan ran in this run and passed T1 with no forced ask; a failure only when the plan caused
    it (a verify or resolve code); anything the person or the session decided is neutral."""
    if replanned:
        return "fail" if _plan_caused(results) else "neutral"
    ran = {r.step_id for r in results}
    complete = ran == {s.id for s in plan.steps} and len(results) == len(plan.steps)
    if complete and all(
        r.ok and r.label_kind == "t1" and not r.evidence.get("ask_reasons") for r in results
    ):
        return "success"
    return "fail" if _plan_caused(results) else "neutral"


def _plan_caused(results: Sequence[StepResult]) -> bool:
    return any(r.code is not None and r.code.value in _PLAN_CAUSED for r in results)
