"""The batch rule (V2 §5; step 3 packet §3): whether the next action of a batch may run.

Interface stub for step 3c; the invariant tests in `tests/loop2/test_invariants_batch.py` are
written first and fail with `NotImplementedError` until 3c implements it.
"""

from __future__ import annotations

from steerai.loop2._stub import stub
from steerai.world.effects import EffectReport


@stub
def batch_continues(report: EffectReport, *, verified_type: bool, same_target: bool) -> bool:
    """True only when the last report's classes are within {`own_state`, `own_value`, `focus`}
    (or the report is `none` after a `type` whose read-back was True), its tokens within
    {`moved`}, nothing is unread, and the next target re-resolved to the same RuntimeId, role and
    name. Reads the class set and the tokens, never `primary`."""
    raise NotImplementedError("3c: batch rule")
