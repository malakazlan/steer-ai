---
name: principal-review
description: Use when a feature schema, component design, interface, or data contract for Steer needs an approve-or-revise decision before implementation, or when a PR changes a contract, a budget, or the safety gate.
---

# Principal review

## Overview

A principal review is a gate, not a discussion. Its output is a verdict with numbered required
changes, and nothing is approved on a claim that was not measured or cited. The reviewer owns three
questions the author usually skips: *is this already built*, *where is the bottleneck*, and *what is
the cheapest correct design that meets the budget*.

**Violating the letter of this contract is violating its spirit.** "Looks fine" is not a verdict.

## Inputs

A feature packet: purpose, interface with types, rules, budget with its source, test plan, prior art,
and (if available) the edge-case table from `edge-case-hunting` and the report from `verifying-by-testing`.
Missing items are a REVISE finding by themselves.

## Procedure

1. **Already built?** Check `docs/design/STEERAI_ARCHITECTURE.md` section 15 and `docs/reference/`
   for internal or external prior art. Search the web for the component class if the packet says
   "none". Name what exists and what this packet adds; if it adds nothing, say so.
2. **Bottleneck first.** Ask where the time or risk actually goes, with a number from a probe or
   benchmark. A budget that ignores the dominant cost (a snapshot, a model call, a readback poll) is
   wrong. Mark every number without a source as **unmeasured**.
3. **Correctness before speed.** Every rule must be a checkable postcondition with a before and after,
   not a presence check. Every failure needs a taxonomy code (design 5.4). Every value crossing a trace
   boundary is typed and redactable; `Any` without a reason is a finding.
4. **Security order.** Secure, reliable, fast, in that order (CLAUDE.md). Anything irreversible passes
   through the gate; nothing sent originates from screen content.
5. **Cheapest correct design.** Strike every field, rule, or call the budget and tests do not need.
   Prefer idempotent verbs, one round trip, fixed templates over free generation.
6. **Deferrals are written, never implied.** Anything deferred goes into the design doc under a new
   version tag, with the evidence that forced it, before approval.

## Verdict contract

```
VERDICT: APPROVE | APPROVE-WITH-CHANGES | REVISE
PRIOR ART: <what exists> → <what this adds>
BOTTLENECK: <dominant cost, number, source>
BLOCKING: 1. ... (each: what, why, the exact change)
NON-BLOCKING: ...
UNMEASURED CLAIMS: ...
DEFERRED → DESIGN DOC: <section/tag or "none">
```

Under 400 words. Blocking items are numbered and each names the change, not the complaint.

## Red flags

| Rationalization | Reality |
|---|---|
| "The team is behind, approve and fix later" | Later never has a gate. Verdict stands. |
| "The budget comes from the design doc" | The doc's numbers were targets until a probe measured them. Cite the probe. |
| "Prior art: none" | It is almost never none. Search before accepting. |
| "Optimize later" | The bottleneck decides the design. Find it now. |
| "Minor, can defer" | Deferred means written into the doc with a tag, or it is not deferred. |

## Common mistakes

- Reviewing prose instead of the interface: read the types and the rules, then the words.
- Accepting a presence check as verification.
- Approving a budget whose p95 cannot contain a call the rules require.
- Confusing "the author tested it by hand" with evidence.
