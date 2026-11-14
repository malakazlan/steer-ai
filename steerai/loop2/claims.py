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
from steerai.world.landmarks import Landmark
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
    "owned_by_combobox",
    "owned_by_controller_for",
    "owned_by_search_landmark",
    "owned_by_parent",
    "landmarks_unread",
    "option_evidence",
    "words_missing",
    "too_short",
    "standard_only",
    "no_record",
    "param_not_sent",
    "inside_composer",
    "composer_not_cleared",
    "composer_unreadable",
    "no_scope",
    "anchor_vanished",
    "scope_churn",
    "not_new",
    "no_match",
    "scope_shifted",
    "not_replaced",
    "duplicate_effect",
    "count_unchanged",
    "pending_status",
    "failed_status",
]
"""Step 4's own tokens, then the strict tier's: `inside_composer` and `composer_not_cleared` are
new in v2 (T1 has no equivalent); `no_scope` to `failed_status` are T1's `Appeared` reasons,
passed through unchanged ([V2.17] item 7)."""
T1_REASONS: Final = (
    "no_scope",
    "anchor_vanished",
    "scope_churn",
    "not_new",
    "no_match",
    "scope_shifted",
    "not_replaced",
    "duplicate_effect",
    "count_unchanged",
    "pending_status",
    "failed_status",
)
"""The failure reasons `verify.t1` gives an `Appeared` (`_appeared_exact`, `_miss`)."""
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
    typed_params: frozenset[str] = frozenset()
    """The params Steer itself typed under this subgoal. [V2.17] item 2: the selectable-option
    rule applies only when the claim's param is one of them (the echo risk)."""


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
    landmarks: Mapping[RuntimeId, Landmark | None] | None = None,
) -> Checked:
    """`content_present` on `after`: the standard rules (§3.1, §3.2) when the subgoal's commit is
    `none`, the strict rules (§3.3) otherwise. `records` are this task's write-ahead records;
    `field_filled` is the live read of `sent.field` (None: unread); `landmarks` each element's
    nearest landmark ancestor on `after` ([V2.13]).

    [V2.21] #9 field-owned: after the option rule ([V2.17] item 2, on top), a cited element is
    field-owned (`under_suggestions`) when any holds, each adding the token of the rule that
    fired: under a typed field's ComboBox ancestor (`owned_by_combobox`); named by its
    ControllerFor when read (`owned_by_controller_for`); sharing its nearest Search landmark
    (`owned_by_search_landmark`); under its parent container unless that parent is the scope
    Document or a Main landmark (`owned_by_parent`). Anything merely below the Document is not.

    [V2.23] B, C: every rule that fires is emitted, in that order; the false-stop count on the
    pooled set reports per rule both "fired" and "fired alone" (rule 4's real cost is where it
    is the only reason). With no landmark map while a field was typed under the subgoal, the
    check fails closed with `landmarks_unread` (never "owned": it was not read); with no typed
    field there is nothing to own. A ControllerFor that was not read is not observed by design
    (it is not in the fetch yet) and never fails closed."""
    raise NotImplementedError("step 4: content_present")


@stub
def strict_reason(t1_reason: str) -> ClaimToken:
    """A T1 `Appeared` failure reason as the strict tier's reject token: passed through
    unchanged ([V2.17] item 7). A reason outside `T1_REASONS` raises ValueError."""
    raise NotImplementedError("step 4: T1 reasons")


@stub
def closes(commit: Commit, proof: Proof) -> ClaimToken | None:
    """Whether `proof` may close a subgoal of `commit`: a standard proof never closes a commit
    subgoal (`standard_only`, §7.1, §8.8); None when it may."""
    raise NotImplementedError("step 4: closing a subgoal")
