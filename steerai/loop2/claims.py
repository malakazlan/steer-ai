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
    "param_unproven",
    "typed_words_only",
    "evidence_missing",
    "evidence_reused",
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
"""Step 4's own tokens ([V2.39]: `param_unproven`, a param no cited element holds on this
observation; [V2.40]: `evidence_reused`, one element cited for two params; [V2.91]:
`typed_words_only`, a param every word of which Steer typed in this task, which
`content_present` never proves: a claim result, no refusal and no strike), then the strict
tier's: `inside_composer` and `composer_not_cleared` are new in v2 (T1 has no equivalent);
`no_scope` to `failed_status` are T1's `Appeared` reasons, passed through unchanged ([V2.17]
item 7)."""
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
    """What the checker knows about one subgoal. Every field is written by code. [V2.72]: the
    subgoal label on an action never decides what counts as evidence; only the strict tier's
    binding to its write-ahead record uses it."""

    params: Mapping[str, GoalSpan]
    commit: Commit
    """The effective commit (§3.3): declared, raised by the dispatched actions' commits."""
    baseline: Snapshot
    """[V2.72], [V2.73]: the task's first full observation, for every subgoal; it never moves
    later, except that a close of another subgoal of the same closing kind sharing a param span
    rebases it to the observation that close was made on. `content_present` closes a page
    subgoal, so it shares `navigated`'s baseline and rebase."""
    typed_fields: tuple[RuntimeId, ...] = ()
    """[V2.38]: every field Steer typed into in the task, whatever subgoal the `type` was
    labelled with (the echo guards are task-wide)."""
    revealed_roots: tuple[RuntimeId, ...] = ()
    """Revealed roots of the task's effect reports since the baseline (§3.2)."""
    controlled: tuple[RuntimeId, ...] = ()
    """Elements named by a typed field's ControllerFor, as read (§3.2; probe P1)."""
    typed_params: frozenset[str] = frozenset()
    """[V2.38], [V2.17] item 2: this subgoal's params whose words are text Steer typed anywhere
    in the task; every param once the task typed generated text (its words are not kept). The
    selectable-option rule applies only when the claim's param is one of them (the echo
    risk)."""
    typed_words: frozenset[str] = frozenset()
    """[V2.91]: the words (casefolded whole tokens, as the word rule reads them) of every text
    Steer typed in this task, from the pending-text records, never the subgoal label. At the
    standard tier a param all of whose words are here is `typed_words_only`."""


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
    """[V2.88] L3: the pending-text record of the field the content was typed into, as the
    checker sees it. It binds by provenance and digest; the subgoal label on the `type` is not
    in it ([V2.72]: a label never decides evidence)."""

    field: RuntimeId
    span: GoalSpan | None
    """The request span the text was typed from (its provenance); None for generated text."""
    read_back: bool | None
    """Whether the keyed digest read right after the verified type equals the span's (True),
    differs (False), or was unread (None)."""


@dataclass(frozen=True)
class Checked:
    """A proof, or the closed reject tokens; never both."""

    proof: Proof | None
    tokens: tuple[ClaimToken, ...]


@stub(4)
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
    `sent` the content field's pending-text record ([V2.88] L3; None: missing, fails closed);
    `field_filled` is the live read of `sent.field` (None: unread); `landmarks` each element's
    nearest landmark ancestor on `after` ([V2.13]).

    [V2.73] (1): `scope_document` is the outermost Document holding the most recently acted
    element in the task, whatever its subgoal label (the [V2.3] observer rule); None when
    nothing was acted on, and then the top Document of `after` (none or several: `no_scope`).
    [V2.39], [V2.40]: every param of the subgoal is held by a cited element on `after`, one
    element per param. [V2.88] L10: a param's words count only in the element's own name, never
    its group name, and text Steer typed (a typed field) never counts. [V2.91]: at the standard
    tier a param every word of which is in `typed_words` is never proven (`typed_words_only`;
    a page can echo them, "No results for ..."); one with a word Steer did not type stays
    provable. At the strict tier the new element's name must equal the typed text after
    folding (T1's full-string `name_matches`), never merely hold its words.

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


@stub(4)
def strict_reason(t1_reason: str) -> ClaimToken:
    """A T1 `Appeared` failure reason as the strict tier's reject token: passed through
    unchanged ([V2.17] item 7). A reason outside `T1_REASONS` raises ValueError."""
    raise NotImplementedError("step 4: T1 reasons")


@stub(4)
def closes(commit: Commit, proof: Proof) -> ClaimToken | None:
    """Whether `proof` may close a subgoal of `commit`: a standard proof never closes a commit
    subgoal (`standard_only`, §7.1, §8.8); None when it may."""
    raise NotImplementedError("step 4: closing a subgoal")
