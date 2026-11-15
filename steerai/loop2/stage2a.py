"""Stage 2's group 2a (stage 2 plan rev 4.4, [V2.63] Q1): the pooled set's local tasks, before
the real sites, with the same harness and gates and no network.

- **The tasks are data.** Group 2a is every pooled-set task (dev, H1-H6) whose start is not a web
  address, less those out of step 3 (`stage2a_out_of_step.json`, each with its reason: a task
  needing a dialog or a second window waits for step 7, listed and not run). The native tasks
  that need neither (plain typing, a calculator's own buttons) are in, with expected outcomes
  rated before the run like every other task.
- **Expected outcomes** come from stage 2's own table builder (`stage2_expected.table_row`), on
  each task's saved start read: a held-out page served on 127.0.0.1, the inbox page with the
  task's own reply, or the native window. The `--save-reads` pass is read-only.
- **Runs** are stage 1's: one task's runs in turn (`run_task_runs`, no spacing, no network),
  each classified by stage 1's `classify`, the pass check read by the v1 suite's own readers,
  never the verifier's. A fixture or app that fails before the child is stage 1's
  infrastructure void; there are no site voids in 2a.

Live: `uv run python -m steerai.loop2.stage2a --save-reads <dir>` (read-only), then
`uv run python -m steerai.loop2.stage2a --table <table.json> --runs 3 --out <dir>
--price-file <prices>` (Windows; real input through the child; only in a cleared window).
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from steerai.loop2.stage1 import (
    FLOOR_MB,
    TASK_CAP_S,
    Classified,
    RunRecord,
    StageTask,
    Summary,
    accept,
    child_argv,
    precheck,
    run_child,
    run_sampled,
    run_task_runs,
    wait_until_ready,
)
from steerai.loop2.stage2_expected import (
    SiteRow,
    TableRow,
    dump_table,
    load_acts,
    load_table,
    load_unrated,
    table_from_reads,
)
from steerai.probes.layer_b_pages import REPLY_TEXT, InvalidRun
from steerai.probes.task_suite import H2, H3, H4, H5, H6, HELD_OUT, TASKS, Task

LABEL: Final = "2a"
ACTS: Final = Path(__file__).with_name("stage2a_acts.json")
"""Each 2a task's acts, by role and name as its saved start read shows them."""
TABLE: Final = Path(__file__).with_name("stage2a_expected.json")
"""The committed 2a table: each read's SHA-256, its deciding facts and the expected outcome."""
TABLE_SHA256: Final = "0e4434e00246eacbcbf209430b4cced63d9eeef5d3329b5f6c08778b4e43fb78"
"""#408 blocker 1: the committed table's pin. A run takes only this table, so moving a task into
`unrated` (out of acceptance) can never be done by a table passed on the command line."""
CALC_WAIT_S: Final = 20.0
"""How long the calculator's window may take to show (unmeasured; the v1 suite's own wait)."""
OUT_OF_STEP_FILE: Final = Path(__file__).with_name("stage2a_out_of_step.json")
_STEP_7: Final = "step 7:"


def _pooled_local() -> tuple[Task, ...]:
    seen: dict[str, Task] = {}
    for task in (*TASKS, *HELD_OUT, *H2, *H3, *H4, *H5, *H6):
        if not task.start.startswith("https://"):
            seen.setdefault(task.task_id, task)
    return tuple(seen.values())


def load_out_of_step(text: str) -> dict[str, str]:
    """`{task_id: "step 7: why"}` for pooled local tasks only; a `ValueError` naming `out of
    step` for anything else."""
    raw = json.loads(text)
    known = {t.task_id for t in _pooled_local()}
    if not isinstance(raw, dict):
        raise ValueError("out of step: not an object")
    for task_id, reason in raw.items():
        if task_id not in known:
            raise ValueError(f"out of step: {task_id} is not a pooled local task")
        if not isinstance(reason, str) or not reason.startswith(_STEP_7):
            raise ValueError(f"out of step: {task_id} needs its step 7 reason")
    return {str(k): str(v) for k, v in raw.items()}


OUT_OF_STEP: Final[Mapping[str, str]] = load_out_of_step(
    OUT_OF_STEP_FILE.read_text(encoding="utf-8")
)


def local_tasks() -> tuple[Task, ...]:
    """Group 2a: the pooled set's local tasks, in set order, less those out of step 3."""
    return tuple(t for t in _pooled_local() if t.task_id not in OUT_OF_STEP)


def start_key(task: Task) -> str:
    """What a task's start read is of: its page set, its app, or the inbox page with the task's
    own reply text (the page holds it, so inbox reads are one per reply)."""
    if task.start == "inbox":
        return f"inbox:{_reply(task)}"
    return task.start


def _reply(task: Task) -> str:
    return next(iter(task.approve)) if task.check == "inbox_send" else REPLY_TEXT


def read_groups(tasks: Sequence[Task]) -> dict[str, list[str]]:
    """The task ids per start read: one read serves them all."""
    groups: dict[str, list[str]] = {}
    for task in tasks:
        groups.setdefault(start_key(task), []).append(task.task_id)
    return groups


def local_stage_tasks(
    table: Mapping[str, TableRow], *, unrated: Mapping[str, str] | None = None
) -> tuple[StageTask, ...]:
    """Each 2a task with its committed expected outcome; a task held for the lead's ruling runs
    expected `unrated` ([V2.65] (2): held to the hard bar, out of the tasks gate). Any other task
    missing from the table is a `ValueError`."""
    held = set(unrated or {})
    rated_and_held = sorted(held & set(table))
    if rated_and_held:
        raise ValueError(f"held tasks with a table row: {rated_and_held}")
    strangers = sorted(held - {t.task_id for t in local_tasks()})
    if strangers:
        raise ValueError(f"held ids that are not 2a tasks: {strangers}")
    missing = [t.task_id for t in local_tasks() if t.task_id not in set(table) | held]
    if missing:
        raise ValueError(f"unrated tasks: {missing}")
    return tuple(
        StageTask(t.task_id, t.goal, "local", t.start, "unrated")
        if t.task_id in held
        else StageTask(
            t.task_id,
            t.goal,
            "local",
            t.start,
            table[t.task_id].expected.outcome,
            reason=table[t.task_id].expected.reason,
        )
        for t in local_tasks()
    )


def check_table(text: str) -> None:
    """#408 blocker 1: the run's table is the committed one, byte for byte (its SHA-256 against
    `TABLE_SHA256`), and its rated set and unrated map are the committed acts' own."""
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != TABLE_SHA256:
        raise ValueError("table: not the committed 2a table")
    acts = ACTS.read_text(encoding="utf-8")
    if set(load_table(text)) != set(load_acts(acts)) or load_unrated(text) != load_unrated(acts):
        raise ValueError("table: its rated and unrated tasks are not the committed acts' own")


@contextlib.contextmanager
def calculator_window(  # noqa: PLR0913 - the title, then each injected platform call
    *,
    title: str,
    windows: Callable[[], Sequence[Any]],  # Any: a top-level window with .title and .hwnd
    launch: Callable[[], object],
    close: Callable[[int], object],
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    wait_s: float = CALC_WAIT_S,
    settle_s: float = 2.0,
) -> Iterator[int]:
    """The calculator's window, opened read-only and closed after (#408 blocker 4): refused when
    one is already open (never the person's), and on `calculator_not_shown` the one this
    launched is still closed if it shows after all. The title-only match is racy with a person
    opening their own in the same seconds (unmeasured)."""
    if any(w.title == title for w in windows()):
        raise InvalidRun("calculator_already_open")
    launch()
    hwnd: int | None = None
    try:
        deadline = clock() + wait_s
        while hwnd is None and clock() < deadline:
            hwnd = next((w.hwnd for w in windows() if w.title == title), None)
            if hwnd is None:
                sleep(0.5)
        if hwnd is None:
            raise InvalidRun("calculator_not_shown")
        sleep(settle_s)
        yield hwnd
    finally:
        if hwnd is None:  # not shown in time: one settle later, close it if it showed after all
            sleep(settle_s)
            hwnd = next((w.hwnd for w in windows() if w.title == title), None)
        if hwnd is not None:
            close(hwnd)


def rate_2a(acts: Mapping[str, SiteRow], reads: Path) -> list[TableRow]:
    """The 2a table from the saved reads (`<reads>/<task_id>.json`); every 2a task not held as
    unrated needs its acts row (`no_acts`)."""
    held = set(load_unrated(ACTS.read_text(encoding="utf-8")))
    missing = [t.task_id for t in local_tasks() if t.task_id not in acts and t.task_id not in held]
    if missing:
        raise ValueError(f"no_acts: {missing}")
    return table_from_reads(acts, reads, tasks=local_tasks())


def summarize(
    tasks: Sequence[StageTask],
    classified: Sequence[Classified],
    *,
    not_started: int,
    runs_per_task: int,
    unrated: Mapping[str, str] | None = None,
) -> Summary:
    """Stage 1's `accept` on stage 2's floor, labelled 2a; the tasks out of step, and those held
    unrated for the lead, are listed."""
    summary = accept(
        tasks,
        classified,
        controls=None,
        runs_per_task=runs_per_task,
        label=LABEL,
        not_started=not_started,
        floor_mb=FLOOR_MB,
    )
    summary.report["out_of_step"] = dict(OUT_OF_STEP)
    summary.report["unrated"] = dict(unrated or {})
    return summary


# ---- the live passes (Windows) -----------------------------------------------------------------


def _read_window(hwnd: int) -> bytes:  # pragma: no cover - live, Windows only
    """The window's full read as the v2 loop takes it, as `world.read_json` bytes."""
    import secrets  # noqa: PLC0415

    from steerai.probes.shadow_v2 import _ComFacts  # noqa: PLC0415 - COM failures as OSError
    from steerai.probes.uia_snapshot import UiaClient  # noqa: PLC0415
    from steerai.world.model import WorldModel  # noqa: PLC0415
    from steerai.world.observe2 import read_observed  # noqa: PLC0415
    from steerai.world.read_json import observed_to_json  # noqa: PLC0415

    client = UiaClient()
    try:
        read = read_observed(
            WorldModel(client).observe(hwnd, cap_s=5.0),
            _ComFacts(client),
            key=secrets.token_bytes(32),
            windows=lambda: (hwnd,),
        )
    finally:
        del client  # its COM objects are released here, on this thread
    return (observed_to_json(read) + "\n").encode("utf-8")


# Any: a context manager yielding (hwnd, port)
def _opened(task: Task) -> Any:
    """The task's start, opened read-only: a page in a throwaway Chrome (port set), or a native
    app this process launched (port None). Only what was launched here is closed."""
    import contextlib  # noqa: PLC0415
    import shutil  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    from steerai.probes import task_suite as suite  # noqa: PLC0415 - live only
    from steerai.probes.chrome_a11y import CHROME_DEFAULT, page_ws_url  # noqa: PLC0415
    from steerai.probes.layer_b import WINDOW, _free_port  # noqa: PLC0415
    from steerai.probes.layer_b_pages import InvalidRun, PageServer  # noqa: PLC0415
    from steerai.probes.present_switch import page_html  # noqa: PLC0415
    from steerai.probes.windows import list_top_windows, wait_window_for_pid  # noqa: PLC0415

    @contextlib.contextmanager
    def page(pages: Mapping[str, str]) -> Any:  # Any: the context's (hwnd, port)
        tmp = Path(tempfile.mkdtemp(prefix="steer-stage2a-"))
        with PageServer(pages) as server:
            port = _free_port()
            proc = suite._launch_one(
                CHROME_DEFAULT,
                tmp / "profile",
                port,
                f"http://127.0.0.1:{server.port}/page.html",
                window_size=WINDOW,
            )
            try:
                window = wait_window_for_pid(proc.pid, image="chrome.exe", process=proc)
                page_ws_url(port)
                suite._settled(port, suite.SETTLE_LIMIT_S)
                time.sleep(1.0)
                yield window.hwnd, port
            finally:
                suite._stop_one(proc, tmp / "profile")
                shutil.rmtree(tmp, ignore_errors=True)

    @contextlib.contextmanager
    def app(exe: Path) -> Any:  # Any: the context's (hwnd, None)
        proc = subprocess.Popen([str(exe)])  # noqa: S603 - a fixed system path
        try:
            yield wait_window_for_pid(proc.pid, image=exe.name, process=proc).hwnd, None
        finally:
            subprocess.run(  # noqa: S603 - fixed argv; only the process launched here
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],  # noqa: S607 - on PATH
                check=False,
                capture_output=True,
            )

    @contextlib.contextmanager
    def calculator() -> Any:  # Any: the context's (hwnd, None)
        with calculator_window(
            title="Calculator",  # the v1 suite's own window title for its calculator task
            windows=list_top_windows,
            launch=lambda: subprocess.Popen([str(suite.CALC)]),  # noqa: S603 - a fixed path
            close=suite._close,
            sleep=time.sleep,
            clock=time.monotonic,
        ) as hwnd:
            yield hwnd, None

    if task.start in suite.PAGE_SETS:
        return page(suite._pages_for(task))
    if task.start == "inbox":
        return page({"/page.html": page_html(_reply(task))})
    if task.start == "notepad":
        return app(suite.NOTEPAD)
    if task.start == "wordpad":
        if not suite.WORDPAD.is_file():
            raise InvalidRun("wordpad_absent")
        return app(suite.WORDPAD)
    if task.start == "calculator":
        return calculator()
    raise ValueError(f"no 2a start for {task.task_id}")


def save_reads(out: Path) -> int:  # pragma: no cover - live, Windows only
    """The `--save-reads` pass: each distinct start once, read-only, no input; a read per task
    id, kept out of the repo."""
    from steerai.probes.memory_guard import (  # noqa: PLC0415 - live only
        EXIT_LOW_MEMORY,
        free_physical_mb,
    )

    out.mkdir(parents=True, exist_ok=True)
    by_id = {t.task_id: t for t in local_tasks()}
    for key, task_ids in read_groups(local_tasks()).items():
        free = free_physical_mb()
        if free < FLOOR_MB:  # the stage 2 floor, before every start opened
            sys.stdout.write(json.dumps({"start": key, "refused": "low_memory", "free_mb": free}))
            sys.stdout.write("\n")
            return EXIT_LOW_MEMORY
        try:
            with _opened(by_id[task_ids[0]]) as (hwnd, _port):
                raw = _read_window(hwnd)
        except InvalidRun as exc:  # #408 blocker 3: a start that cannot open is a row, as in 2b
            sys.stdout.write(json.dumps({"start": key, "invalid": str(exc)}) + "\n")
            continue
        except (OSError, TimeoutError, RuntimeError) as exc:
            sys.stdout.write(json.dumps({"start": key, "error": type(exc).__name__}) + "\n")
            continue
        for task_id in task_ids:
            (out / f"{task_id}.json").write_bytes(raw)
        sys.stdout.write(json.dumps({"start": key, "tasks": task_ids}) + "\n")
    return 0


def _observed(task: Task, hwnd: int, port: int | None) -> str:  # pragma: no cover - live
    """The v1 suite's own read of the task's end state (no code shared with the verifier)."""
    from steerai.probes import task_suite as suite  # noqa: PLC0415 - live only

    if task.check == "inbox_send" and port is not None:
        raw = suite._read(port, 'localStorage.getItem("steer-present") || "[]"', "read")
        return suite.inbox_send(raw)
    if task.check == "inbox_chat" and port is not None:
        return suite._read(port, "document.body.dataset.chat", "chat")
    if port is not None:
        return suite._page_observed(task, port)
    if task.check == "calc_display":
        return suite.calc_display(suite._calc_names(hwnd))
    return suite.notepad_text(hwnd, "RICHEDIT50W" if task.start == "wordpad" else "Edit")


def _live_local(  # pragma: no cover - live
    task: Task, stage: StageTask, run: int, *, out_dir: Path, price: Path
) -> RunRecord:
    """One 2a run: the start opened, the child on its window, then the v1 suite's read."""
    from steerai.loop2.stage1 import _start, _tree_reading  # noqa: PLC0415 - live only
    from steerai.probes.layer_b_pages import InvalidRun  # noqa: PLC0415
    from steerai.probes.suite_chunks import kill_tree  # noqa: PLC0415
    from steerai.probes.task_suite import passed  # noqa: PLC0415

    run_dir = out_dir / f"{task.task_id}-{run}"
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        opened = _opened(task)
        with opened as (hwnd, port):
            ran, memory = run_sampled(
                lambda: run_child(
                    child_argv(stage, hwnd=hwnd, price=price, trace=run_dir / "trace.jsonl"),
                    cap_s=TASK_CAP_S, out=run_dir / "final.jsonl", start=_start, kill=kill_tree,
                ),
                read=_tree_reading,
            )  # fmt: skip
            try:
                check: bool | None = passed(task, "done", _observed(task, hwnd, port))
                log_read = True
            except (InvalidRun, OSError, RuntimeError, ValueError):
                check, log_read = None, False
            return RunRecord(task.task_id, run, ran.final, True, log_read, check, None,
                             killed=ran.killed, code=ran.code, memory=memory,
                             kill_failure=ran.kill_failure)  # fmt: skip
    except (InvalidRun, OSError, TimeoutError, RuntimeError):
        return RunRecord(task.task_id, run, None, False, False, None, None)


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - live, Windows only
    from steerai.loop2.stage1 import _emit, _idle_s  # noqa: PLC0415 - live only
    from steerai.probes.memory_guard import free_physical_mb  # noqa: PLC0415

    parser = argparse.ArgumentParser(prog="python -m steerai.loop2.stage2a")
    parser.add_argument("--save-reads", type=Path, help="live, read-only: save each start read")
    parser.add_argument("--rate", type=Path, help="the saved reads: write the 2a table")
    parser.add_argument("--table", type=Path)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--price-file", type=Path)
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.save_reads is not None:
        return save_reads(args.save_reads)
    if args.rate is not None:
        text = ACTS.read_text(encoding="utf-8")
        rows = rate_2a(load_acts(text), args.rate)
        TABLE.write_bytes(dump_table(rows, unrated=load_unrated(text)).encode("utf-8"))
        return 0
    if args.table is None or args.out is None or args.price_file is None:
        parser.error("--table, --out and --price-file are required for a run")
    table_text = args.table.read_text(encoding="utf-8")
    check_table(table_text)  # #408 blocker 1: only the committed table runs
    unrated = load_unrated(table_text)
    stages = local_stage_tasks(load_table(table_text), unrated=unrated)
    suites = {t.task_id: t for t in local_tasks()}
    classified: list[Classified] = []
    not_started = 0
    for stage in stages:

        def attempt(run: int, stage: StageTask = stage) -> RunRecord | str:
            ready = wait_until_ready(
                lambda: precheck(free_mb=free_physical_mb, idle_s=_idle_s, floor_mb=FLOOR_MB),
                sleep=time.sleep,
                clock=time.monotonic,
            )
            if ready is not None:
                return ready
            return _live_local(
                suites[stage.task_id], stage, run, out_dir=args.out, price=args.price_file
            )

        runs, refused = run_task_runs(
            stage, runs=args.runs, attempt=attempt, emit=_emit, row={"floor_mb": FLOOR_MB}
        )
        classified += runs
        not_started += refused
    summary = summarize(
        stages,
        classified,
        not_started=not_started,
        runs_per_task=max(args.runs, 3),
        unrated=unrated,
    )
    _emit({"summary": {"accepted": summary.accepted, "gates": summary.gates, **summary.report}})
    return 0 if summary.accepted else 1


if __name__ == "__main__":  # pragma: no cover - live
    raise SystemExit(main())
