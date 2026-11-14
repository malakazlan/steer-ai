"""The code verifier's evidence rule for step 3 ([V2.3]: cited evidence must be inside the scope
Document; a tab name or the address bar is never evidence).

Interface stub for step 3c; the tests in `tests/loop2/test_invariants_verify.py` come first.
"""

from __future__ import annotations

from steerai.loop2.decision import Claim
from steerai.world.model import Snapshot


def evidence_tokens(
    claim: Claim, snapshot: Snapshot, *, scope_document: tuple[int, ...] | None
) -> tuple[str, ...]:
    """Reject tokens for the claim's cited ids in `snapshot`: `outside_scope` when an id is not
    inside the scope Document (when there is one), `id_not_listed` when it is not in the
    snapshot. Empty when every cited element is admissible."""
    raise NotImplementedError("3c: evidence scope")
