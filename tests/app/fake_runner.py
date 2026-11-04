"""A stand-in for `python -m steerai.run` in the app's tests: prints what the real runner prints
(a GO line for the plan preview, an ASK block for a step), reads the answers on stdin, and ends
with one JSON line on stdout. Arguments select the script."""

from __future__ import annotations

import json
import sys


def main() -> int:
    script = sys.argv[1] if len(sys.argv) > 1 else "go-then-ask"
    err = sys.stderr
    if script == "crash":
        err.write("internal error: boom\n")
        err.flush()
        sys.stdout.write(json.dumps({"status": "error", "detail": "internal_error"}) + "\n")
        return 70
    err.write("task t-1: window 0x10 pid 500\n")
    err.write("plan 0123456789ab: send hello there to Amir\n")
    err.write("  step s1: type Edit \"Message\" R1 text='hello there'\n")
    err.write("  GO 0123456789ab: type go:0123456789ab to run the plan shown above, or no\n")
    err.flush()
    line = sys.stdin.readline().rstrip("\r\n")
    if line != "go:0123456789ab":
        err.write("plan not approved (only the exact go token counts); nothing was run\n")
        err.flush()
        sys.stdout.write(json.dumps({"status": "paused", "detail": "plan_unanswered"}) + "\n")
        return 2
    if script == "which":
        err.write("ASK d-2 which R2 ListItem\n")
        err.write("  the plan names: Amir\n")
        err.write('  1) ListItem "Amir Khan"  preview: "ok"\n')
        err.write('  2) ListItem "Amir Ali"  preview: "hi"\n')
        err.write(
            "  the target window holds focus until you answer here; "
            "type <n>:d-2 to choose a row, no for none of these\n"
        )
    else:
        err.write("ASK d-1 send R2 Button\n")
        err.write("  target: Send\n")
        err.write("  text: hello there\n")
        err.write('  expect: {"appeared": {"name": "hello there", "role": "Text"}}\n')
        err.write(
            "  the target window holds focus until you answer here; "
            "type yes:d-1 to approve, no to refuse\n"
        )
    err.flush()
    line = sys.stdin.readline().rstrip("\r\n")
    expected = "2:d-2" if script == "which" else "yes:d-1"
    if line == expected:
        err.write("step done\n")
        err.flush()
        sys.stdout.write(json.dumps({"status": "done", "ticked": ["g1"], "answer": line}) + "\n")
        return 0
    sys.stdout.write(
        json.dumps({"status": "blocked", "detail": "user_declined", "answer": line}) + "\n"
    )
    return 3


if __name__ == "__main__":
    sys.exit(main())
