# Steer

**A fast, accurate, reliable computer-use agent for Windows.**

You tell your computer "DM Amir hello" or "email the latest invoice to Sara", and it does it: opens the
apps, clicks, types and sends, like a person at your desk. It reads the screen's structure instead of
screenshots, decides routine steps in milliseconds, verifies every action, remembers what worked, and
never does anything risky without asking.

> Status: design complete, build starting (September 2026). Nothing here runs yet.

## Why

Today's computer agents are slow (a screenshot to a large model on every step), flaky (coordinate misclicks
that compound), blind to their own mistakes (they assume success), and unsafe by default (screen text can
inject instructions). Steer is built around four bets:

- **Structure before pixels.** The Windows UI Automation tree is the primary sense. OCR, icon matching and
  vision are fallbacks. No images leave the machine.
- **System 1 / System 2.** [Jev](https://typesafe.ai), a typed decision model, answers the routine
  questions on every step (which element, which action, is this on goal) in 70–500 ms. A frontier LLM is
  called only to plan a new task or rescue a stuck one.
- **Verify everything.** Every action carries an expected effect checked against a before/after diff of
  the screen. Tasks are goal checklists, so recovery resumes from the real state and never sends twice.
- **Learn from outcomes.** A scored cache of elements, screens, decisions and whole skills means the fifth
  run of a task needs zero model calls.

Safety sits on top of all of it: deterministic hard rules, risk tiers (read / reversible / external /
destructive), provenance checks so that what gets sent came from you and not from the screen, and a
confirmation overlay for anything irreversible.

## How it fits together

```
user request
   │
   ▼
Intent Router (Jev) ──► Planner (LLM, rare) ──► checklist of verifiable subgoals
   │
   ▼
Orchestrator: for each step  lookup → decide → gate → act → verify → learn
   │            cache          Jev     rules   OS     diff    cache
   ▼
Perception: UIA tree ► OCR ► icon match ► vision      (local, CPU)
World Model: current screen state, landmarks, diffs
Input: real keyboard and mouse at human pace, or native UIA actions
```

## Stack

| Part | Language |
|---|---|
| Core: UIA, capture, input, world model, cache store | Rust (Phase 5), Python prototype first |
| Brain: orchestrator, planner, verifier, safety gate, learning | Python 3.12 |
| Bindings | PyO3 |
| Fast decisions | Jev via API |
| Planning | frontier LLM via API, swappable |

## Roadmap

| Phase | Goal |
|---|---|
| 0 | Probe: measure UIA latency, Chrome behaviour, capture, input, Jev accuracy on real element lists |
| 1 | Python MVP: perception + world model + Jev selector + input + step verification, assist mode |
| 2 | Brain: planner, checklists, recovery and resume, goal verification |
| 3 | Safety: risk tiers, hard rules, provenance, confirmations, audit |
| 4 | Memory: scored cache and skills, zero-call repeat tasks |
| 5 | Rust core behind PyO3 |
| 6 | Background mode (isolated session), post-v1 |
| 7 | Learning: tuning from verified traces |
| 8 | Eval on Windows Agent Arena and OSWorld-Verified, release |

## Development

Requirements: Windows 11 (Windows 10 22H2 best-effort), [uv](https://docs.astral.sh/uv/), Docker
(optional, Linux-safe unit tests only), a TypeSafe API key for live Jev tests.

```
uv sync                      # creates .venv with Python 3.12 and all dev tools
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest                # Windows integration tests run only on Windows
```

Every change goes through a pull request and must be green in CI before merge. See `CONTRIBUTING.md`.

## License

To be decided before the first public release.
