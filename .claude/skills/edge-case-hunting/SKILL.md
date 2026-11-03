---
name: edge-case-hunting
description: Use when a Steer step, action, verifier rule, cache policy, or safety rule is being designed or implemented and someone needs the failure modes enumerated before tests are written; also when a bug suggests a class of cases was missed.
---

# Edge-case hunting

## Overview

An edge case is only useful to the tester when it has an id, a trigger, an expected behaviour and a
severity. A list of worries is not an edge-case analysis. Enumerate by dimension so coverage is
systematic, then rank, then hand over a table the tester can execute line by line.

## Dimensions (walk every one; "none" is an answer)

| Dimension | Ask |
|---|---|
| Input domain | empty, whitespace, max length, Unicode and IME, newlines, duplicates, already-present values |
| Identity | two matches, zero matches, twin controls, localized or redacted names, element replaced by an identical one |
| State | precondition already satisfied (idempotency), disabled, read-only, minimized, covered, off-screen, virtualized |
| Timing | not settled yet, Chromium first contact, WPF early read, app slow, timeout during delivery, readback arrives late |
| Concurrency | user input mid-step, focus stolen, notification, second window of the same app, parallel agent |
| Platform | DPI and multi-monitor, elevated target (UIPI), secure desktop, virtual desktop, Windows 10 vs 11 |
| Delivery | not delivered, uncertain, delivered unverified, delivered verified; retry safety for each |
| Resume | crash before, during, after the action; write-ahead record present or missing; effect already present from an earlier run |
| Security | screen content as instruction (injection), parameter provenance, secrets in traces, password fields, blocked shortcuts, protected processes |
| Budget | model-call cap, cost cap, step cap, loop guard on repeated screens |
| Verification | effect present before the action, normalized by the app, placeholder states ("sending"), effect in the wrong container |

## Output contract

```
| id | dimension | trigger | expected behaviour | severity | test type |
| SM-07 | Verification | identical "hello" bubble already in history | pass only if a bubble absent from the before snapshot appears | S1 | unit (fixture diff) |
```

- `severity`: S1 wrong irreversible action or false success; S2 wrong reversible action or stuck;
  S3 degraded; S4 cosmetic.
- `test type`: unit, windows, probe, live, or design (needs a rule, not a test).
- Sort S1 first. Mark every S1 that the current design does not handle as **DESIGN GAP** and say which
  section should own it.
- Under 60 rows for a single step; split larger scopes.

## Red flags

| Rationalization | Reality |
|---|---|
| "Quick list, we need it in minutes" | A list without expected behaviour cannot be tested. Walk the table. |
| "Security is the gate's job" | The gate is designed from these cases. Enumerate them here. |
| "Timing is flaky, skip it" | Timing cases are the ones that ship bugs. Give them triggers. |
| "Obvious cases don't need rows" | Obvious to you is untested for everyone else. |

## Common mistakes

- Listing symptoms ("focus lost") without the trigger that produces them.
- Forgetting that the effect may already exist from a previous run.
- Treating "present after" as success; the check is "absent before, present after".
- Omitting the retry-safety consequence of each delivery outcome.
