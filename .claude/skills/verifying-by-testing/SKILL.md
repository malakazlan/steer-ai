---
name: verifying-by-testing
description: Use when asked to test, confirm, sign off, or declare done any Steer change, when a teammate says "it works on my machine", when a test is flaky or slow, or when a budget or latency claim needs proof.
---

# Verifying by testing

## Overview

Nothing is done until a command ran and its output is in the report. The tester's job is to make a
claim fail: write the test that would expose the bug, run it, and show the result. A green run that
was not watched failing proves nothing.

**REQUIRED SUB-SKILL:** superpowers:test-driven-development. **REQUIRED before any "done":**
superpowers:verification-before-completion.

## Procedure

1. **Take the edge-case table** (from `edge-case-hunting`) or build one. Every S1 and S2 row gets a
   test or a written reason it cannot have one.
2. **Red first.** Write the test, run it, paste the failing output. If it passes immediately, the test
   is wrong or the behaviour already exists; say which.
3. **Determinism.** No `sleep`, no wall clock, no network, no real desktop in unit tests. Inject the
   clock and the reader. Real desktop work is `@pytest.mark.windows`; paid APIs are `live`; app
   launches are `probe`.
4. **Boundaries.** For every numeric parameter: zero, one, the limit, the limit plus one, negative.
   For every optional: absent and present. For every collection: empty, one, many, duplicates.
5. **Flakiness repeat.** Run the new tests five times consecutively. One failure in five is a bug in
   the code or the test, never a retry.
6. **Budgets are assertions.** A latency claim becomes a measured number from a probe, compared to the
   design doc section 9 budget, p50 and p95, in the report. No number, no claim.
7. **Gates.** `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest`
   with `set -o pipefail`; never pipe pytest into a filter without it.
8. **Report** with the contract below. Verbatim output, not paraphrase.

## Report contract

```
STATUS: GREEN | RED | BLOCKED
RED SEEN: <test name → failing output, before the fix>
GATES: <each command → last line of output>
REPEAT: 5/5 | n/5 (names of failures)
BUDGET: <measured p50/p95 vs section 9 target, or "no latency claim">
COVERAGE: <edge-case ids covered / total S1+S2>
NOT TESTED: <ids and why>
```

## Red flags

| Rationalization | Reality |
|---|---|
| "Already ran it by hand" | No record, cannot re-run. Not evidence. |
| "CI is slow, just say it looks good" | CI green is the merge condition. Wait. |
| "Tests after will catch the same bugs" | Tests after pass immediately and prove nothing. |
| "Flaky once, passed on rerun" | A rerun hides a bug. Find it. |
| "It's a two-line change" | Two-line changes break budgets and gates. Run them. |

## Common mistakes

- Asserting on mocks instead of behaviour.
- `pytest.raises(Exception)` or a bare `OSError`: assert the exact type and the taxonomy code, or
  `match=` on a stable message prefix, so an unrelated crash cannot pass the test.
- Testing the happy path of an idempotent verb but not the already-satisfied path.
- Marking a Windows test as unit so Linux CI passes while nothing ran.
- Claiming a budget from one run; p95 needs at least five.
