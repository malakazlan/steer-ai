"""PreToolUse guard for Bash commands: enforces the workflow rules in CLAUDE.md.

Blocks (exit code 2, reason on stderr):
  - any --no-verify
  - git push --force / -f (use --force-with-lease on your own feature branch only)
  - git commit or git merge while the current branch is main
  - git push that targets main from the main branch

Reads the hook payload from stdin. Stdlib only; runs on any Python 3.8+.
"""

import json
import re
import shutil
import subprocess
import sys


def current_branch() -> str:
    """Return the checked-out branch name, or "" if it cannot be determined.

    Uses symbolic-ref so it also works on an unborn branch (repo with no commits).
    """
    git = shutil.which("git")
    if git is None:
        return ""
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, no user input
            [git, "symbolic-ref", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def block(reason: str) -> None:
    sys.stderr.write("BLOCKED by .claude/hooks/guard.py: " + reason + "\n")
    sys.exit(2)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return
    command = str(payload.get("tool_input", {}).get("command", ""))
    if not command:
        return

    if "--no-verify" in command:
        block("--no-verify is never allowed; fix the hook failure instead.")

    is_push = re.search(r"\bgit\s+push\b", command) is not None
    if is_push and re.search(r"(\s--force(?=\s|$)|\s-f(?=\s|$))", command):
        block("git push --force / -f is not allowed; --force-with-lease on your own branch only.")

    if re.search(r"\bgit\s+(commit|merge)\b", command) and current_branch() == "main":
        block("current branch is main; create a branch and open a PR instead.")

    if is_push and re.search(r"\bmain\b", command) and current_branch() == "main":
        block("pushing main directly is not allowed; changes reach main only through a green PR.")


if __name__ == "__main__":
    main()
