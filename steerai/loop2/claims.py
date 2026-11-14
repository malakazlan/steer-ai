"""Claims with evidence for step 4 (V2 §7.1, [V2.3], [V2.15]): `content_present` at the standard
tier, and the strict tier for commit subgoals, bound to the write-ahead record (§8.8).

Interface stub for step 4; the tests in `tests/loop2/test_invariants_claims.py` and
`tests/loop2/test_invariants_strict.py` come first (packet v2-step4-claims §3.1 to §3.3).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from steerai.loop2._stub import stub
from steerai.loop2.decision import Claim
from steerai.loop2.task import Commit, GoalSpan
from steerai.world.model import Snapshot

RuntimeId = tuple[int, ...]
Tier = Literal["exact", "standard", "strict"]
Phase = Literal["written", "acting", "verified"]
ClaimToken = Literal[
    "param_missing",
    "evidence_missing",
    "too_much_evidence",
    "id_not_listed",
    "outside_scope",
    "not_in_delta",
    "inside_field",
    "under_suggestions",
    "option_evidence",
    "words_missing",
    "too_short",
    "standard_only",
    "no_record",
    "param_not_sent",
    "preexisting",
    "duplicate_effect",
    "partial_content",
    "inside_composer",
    "composer_not_cleared",
    "composer_unreadable",
]
Signal = Literal["in_delta", "new_top_document", "under_revealed"]
MAX_EVIDENCE: Final = 4
"""The most ids one `content_present` claim may cite (§3.1 rule 2)."""


@dataclass(frozen=True)
class Proof:
    """V2 §6.5: a claim that code accepted, with the tier it was checked at."""

    claim: Claim
    tier: Tier
    signals: frozenset[Signal]
    preexisting: bool
    action_id: str | None
    """The write-ahead record's action key; always set at the strict tier (§8.8)."""


@dataclass(frozen=True)
class SubgoalFacts:
    """What the checker knows about one subgoal. Every field is written by code."""

    params: Mapping[str, GoalSpan]
    commit: Commit
    """The effective commit (§3.3): declared, raised by the dispatched actions' commits."""
    baseline: Snapshot
    """The snapshot before the subgoal's first action; the task's first one if it has none."""
    typed_fields: tuple[RuntimeId, ...] = ()
    """Fields a `type` of this subgoal went into."""
    revealed_roots: tuple[RuntimeId, ...] = ()
    """Revealed roots of this subgoal's effect reports since the baseline (§3.2)."""
    controlled: tuple[RuntimeId, ...] = ()
    """Elements named by a typed field's ControllerFor, as read (§3.2; probe P1)."""

    @property
    def search(self) -> bool:
        """[V2.15] Q4 as read in packet §9: a commit-`none` subgoal with a `type` into a field."""
        return self.commit == "none" and bool(self.typed_fields)


@dataclass(frozen=True)
class CommitRecord:
    """A write-ahead record as the claim checker sees it ([V2.15] Q1): the subgoal id is in the
    record, never in the key."""

    action_key: str
    subgoal: str
    phase: Phase
    before: Snapshot


@dataclass(frozen=True)
class Sent:
    """The subgoal's last verified `type`: the param it used and the field it went into."""

    param: str
    field: RuntimeId


@dataclass(frozen=True)
class Checked:
    """A proof, or the closed reject tokens; never both."""

    proof: Proof | None
    tokens: tuple[ClaimToken, ...]


@stub
def check_content(  # noqa: PLR0913 - the claim, its screens, and keyword-only facts
    claim: Claim,
    after: Snapshot,
    subgoal: SubgoalFacts,
    *,
    scope_document: RuntimeId | None,
    records: Sequence[CommitRecord] = (),
    sent: Sent | None = None,
    field_filled: bool | None = None,
) -> Checked:
    """`content_present` on `after`: the standard rules (§3.1, §3.2) when the subgoal's commit is
    `none`, the strict rules (§3.3) otherwise. `records` are this task's write-ahead records;
    `field_filled` is the live read of `sent.field` (None: unread)."""
    raise NotImplementedError("step 4: content_present")


@stub
def closes(commit: Commit, proof: Proof) -> ClaimToken | None:
    """Whether `proof` may close a subgoal of `commit`: a standard proof never closes a commit
    subgoal (`standard_only`, §7.1, §8.8); None when it may."""
    raise NotImplementedError("step 4: closing a subgoal")
