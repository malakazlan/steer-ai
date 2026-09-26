# CLAUDE.md — Steer working rules

Steer is a Windows-first computer-use agent: UIA-first perception, Jev (TypeSafe System One) for
fast typed decisions, a frontier LLM planner, verified actions, a scored cache, and a hard safety gate.
The design of record is `docs/design/STEERAI_ARCHITECTURE.md` (local only, see "Folders").

## Constraints, in priority order

1. **Secure.** Nothing the agent does may be irreversible without the gate. No secrets in code, logs,
   traces or tests. No `shell=True`, no `eval`, no dynamic imports from data. Validate every input at a
   process or network boundary. Cache and traces that contain user data are encrypted with DPAPI.
2. **Reliable.** Every action has a checkable postcondition. Unclear means fail. Tests are deterministic;
   a flaky test is a bug to fix, never to retry or skip.
3. **Fast.** Measure before optimizing. Budgets live in the design doc section 9; a change that misses a
   budget is not done. Hot paths are profiled, not guessed.

## Workflow (no exceptions)

- **Never commit to `main` directly.** Every change is a branch → PR → CI green → merge.
- Branch names: `feat/<area>-<short>`, `fix/<area>-<short>`, `chore/<short>`, `probe/<short>`.
- **Stacked PRs are allowed**: branch B from branch A, open B's PR against A. Merge bottom-up. Rebase the
  stack after each merge. Say in the PR body which PR it stacks on.
- Work on several PRs in parallel while CI runs; never idle waiting on CI when there is independent work.
- **Merge only when CI is green.** Either party (Claude or the user) may merge a green PR. Squash-merge.
- Conventional commits (`feat:`, `fix:`, `test:`, `chore:`, `docs:`, `perf:`, `refactor:`). One logical
  change per commit. Never `--no-verify`, never force-push a shared branch, never amend a pushed commit.
- Every PR description states: what, why, how it was verified (paste the command and result), and what is
  intentionally out of scope. Add the attribution line the session requires.
- Before claiming anything is done: run the verification and show the output. No "should work".

## Code standards

- Python 3.12, managed with `uv` (`uv sync`, `uv run ...`). Never `pip install` into the system Python.
- `ruff` (lint + format) and `mypy --strict` must pass. Type every public function. No `Any` without a
  comment saying why. No `# type: ignore` without an error code and a reason.
- **Tests first.** Write the failing test, make it pass, refactor. Unit tests are pure and fast; Windows
  integration tests are marked `@pytest.mark.windows` and skipped elsewhere. Anything that talks to Jev or
  an LLM is mocked in unit tests and lives behind a `live` marker for opt-in runs.
- No bare `except`. No swallowed exceptions. Errors carry the taxonomy code from the design doc section 5.4.
- Logging is structured (JSON lines), never prints. Redact names, message text and file paths at the
  trace boundary unless the trace level explicitly allows them.
- Windows specifics: the process is Per-Monitor-V2 DPI aware; the UIA client runs on an MTA thread with
  connection and transaction timeouts set; every `SendInput` call tags `dwExtraInfo` with Steer's magic value.
- Dependencies: pinned via `uv.lock`. Adding one requires a sentence in the PR on why and what it pulls in.
- No dead code, no commented-out code, no TODOs without an issue number.

## No assumptions

- Platform facts (Windows APIs, Chrome behaviour, Jev limits) are verified against official docs
  (use the context7 docs tool or a web search) or measured, and the source is cited in the PR or the doc.
- Numbers come from a probe or a benchmark, never from memory. If unmeasured, say "unmeasured".
- If the design doc and reality disagree, reality wins: fix the doc in the same PR under a new tag.

## Skills to use

- `superpowers:test-driven-development` for every feature or fix.
- `superpowers:systematic-debugging` before proposing any fix for a failure.
- `superpowers:verification-before-completion` before saying anything is done or green.
- `superpowers:requesting-code-review` / `code-review` before merging non-trivial PRs.
- `superpowers:brainstorming` before any new component not already specified in the design doc.

## Folders

- `steerai/` Python package. `tests/` pytest. `.github/workflows/` CI, the source of truth for green.
- `docs/` **local only, gitignored.** `docs/design/` holds the architecture and every design `.md`.
- `context/` **local only, gitignored.** Session hand-off notes so the user can clear or compact the
  session and resume fresh. Update `context/CURRENT.md` at the end of every working session, before the
  context gets compacted, and whenever a decision is made that is not yet in the design doc.
  Snapshot to `context/YYYY-MM-DD.md` at the end of each day.
- Docker is for running the Linux-safe unit tests locally in a clean environment; GitHub CI on
  `windows-latest` is where the real tests run.

## Communication

- Lead with the outcome. Report test failures verbatim. Say what was skipped and why.
- Do not agree by default and do not object by default. State facts, flag unverified claims as such.
