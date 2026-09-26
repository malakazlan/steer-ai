# Contributing

## Workflow

1. Branch from `main`: `feat/<area>-<short>`, `fix/<area>-<short>`, `chore/<short>`, `probe/<short>`.
2. Write the failing test first, then the code, then refactor.
3. Run locally before pushing:
   ```
   uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
   ```
4. Open a PR. The description states what, why, how it was verified (command and output), and what is
   out of scope. Stacked PRs say which PR they stack on.
5. CI must be green. Squash-merge. Delete the branch.

Never push to `main` directly, never `--no-verify`, never force-push a shared branch.

## Standards

- Python 3.12, `uv` for everything. `ruff` and `mypy --strict` are gates, not suggestions.
- Conventional commits. One logical change per commit.
- No secrets anywhere in the repo, ever. `.env` is gitignored; CI secrets live in GitHub.
- Windows integration tests are marked `windows`; tests that call Jev or an LLM are marked `live` and are
  opt-in (`uv run pytest -m live`).
- Jev access: set `TYPESAFE_API_KEY`. Without a TypeSafe key, an OpenRouter key works with
  `STEER_JEV_BASE_URL=https://openrouter.ai/api` and `STEER_JEV_MODEL=typesafe/jev-1.13`. A local
  Jev-compatible server works the same way with its own base URL and model name.
- Platform facts are cited from official documentation or measured. Numbers are measured.

## Local-only folders

`docs/` and `context/` are gitignored on purpose. Design documents and session hand-off notes stay on the
developer's machine.
