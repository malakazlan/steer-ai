"""Stage 2's expected outcomes (packet v2-ports-adapter, stage 2 plan rev 4.4 §1.2): computed by
stage 1's `rate` and `outcome_of` from each task's saved start-page read, with the acts as data.
Only each read's SHA-256 and its deciding facts are committed; the reads stay local."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from steerai.loop2.stage1_expected import Act, Expected
from steerai.loop2.stage2_expected import (
    SITE_SPACING_S,
    DecidingFacts,
    SiteRow,
    Spacer,
    StartDocument,
    deciding_facts,
    dump_table,
    load_acts,
    load_table,
    real_site_tasks,
    site_check,
    start_document,
    table_row,
)
from steerai.world.effects import Observed
from steerai.world.landmarks import Landmark
from steerai.world.read_json import observed_from_json, observed_to_json
from tests.loop2.world_fakes import DOC, FIELD, GO, element, observed

FORM = element(40, "Group", "", (9, 40), 1)
BOX = element(41, "Edit", "Find", (9, 41), 40)
GO_BOX = element(42, "Button", "Look up", (9, 42), 40)
NOTE = element(43, "Edit", "Notes", (9, 43), 1)
PRIVATE = element(44, "Text", "a private headline only the page has", (9, 44), 1)
SHOWN = [DOC, FIELD, GO, FORM, BOX, GO_BOX, NOTE, PRIVATE]
GOAL = "search the shop for red shoes"

TYPE_ENTER = SiteRow(
    acts=(Act("type", "Edit", "Find", param="red shoes"), Act("press", "", "", key="Enter")),
    proof="navigated",
    why="the results page's URL carries the words",
)
TYPE_CLICK = SiteRow(
    acts=(Act("type", "Edit", "Find", param="red shoes"), Act("click", "Button", "Look up")),
    proof="navigated",
    why="the results page's URL carries the words",
)


def read(*, searchbox: bool = True, landmark: bool = True) -> Observed:
    marks: dict[tuple[int, ...], Landmark | None] = {e.runtime_id: None for e in SHOWN}
    if landmark:
        search = Landmark(kind="search", runtime_id=FORM.runtime_id)
        marks |= {BOX.runtime_id: search, GO_BOX.runtime_id: search}
    roles = {BOX.runtime_id: "searchbox"} if searchbox else {}
    return dataclasses.replace(observed(SHOWN), landmarks=marks, aria_roles=roles)


def saved(seen: Observed) -> bytes:
    return (observed_to_json(seen) + "\n").encode("utf-8")


# ---- the deciding facts ------------------------------------------------------------------------


def test_the_deciding_facts_are_the_fields_role_landmark_button_and_risk() -> None:
    facts = deciding_facts(TYPE_CLICK, GOAL, read())
    assert facts == DecidingFacts(
        field_role="Edit",
        aria_role="searchbox",
        search_landmark=True,
        button_role="Button",
        button_search_landmark=True,
        risk="R1",
    )


def test_an_enter_has_no_button() -> None:
    facts = deciding_facts(TYPE_ENTER, GOAL, read())
    assert facts.button_role is None
    assert facts.button_search_landmark is None


def test_an_unread_aria_role_is_none_never_a_guess() -> None:
    facts = deciding_facts(TYPE_ENTER, GOAL, dataclasses.replace(read(), aria_roles=None))
    assert facts.aria_role is None


# ---- the expected outcome: stage 1's rule, unchanged -------------------------------------------


def test_a_query_class_field_with_a_proof_is_expected_done() -> None:
    row = table_row("t1", TYPE_ENTER, GOAL, saved(read()))
    assert row.expected == Expected("t1", "done", None, "R1", "navigated", TYPE_ENTER.why)


def test_a_field_that_is_not_query_class_is_expected_blocked_at_the_commit() -> None:
    row = table_row("t1", TYPE_ENTER, GOAL, saved(read(searchbox=False, landmark=False)))
    assert row.expected.outcome == "blocked"
    assert row.expected.reason == "commit_not_in_step"
    assert row.facts.risk == "R2"


def test_no_proof_is_expected_never_done() -> None:
    row = table_row("t1", dataclasses.replace(TYPE_ENTER, proof=None), GOAL, saved(read()))
    assert row.expected.outcome == "never_done"


def test_a_target_missing_from_the_read_is_an_error_never_a_guess() -> None:
    gone = SiteRow((Act("type", "Edit", "Not here", param="x"),), "navigated", "why")
    with pytest.raises(ValueError, match="not_in_read"):
        table_row("t1", gone, GOAL, saved(read()))


# ---- what is committed: digests and deciding facts only ----------------------------------------


def test_the_table_holds_the_reads_digest_and_never_its_text() -> None:
    raw = saved(read())
    row = table_row("t1", TYPE_CLICK, GOAL, raw)
    text = dump_table([row])
    assert row.read_sha256 == hashlib.sha256(raw).hexdigest()
    assert row.read_sha256 in text
    assert "private headline" not in text
    assert load_table(text) == {"t1": row}


def test_the_table_text_is_stable() -> None:
    rows = [table_row(t, TYPE_ENTER, GOAL, saved(read())) for t in ("b2", "a1")]
    assert dump_table(rows) == dump_table(list(reversed(rows)))
    assert list(json.loads(dump_table(rows))) == ["a1", "b2"]


# ---- the site-changed check (stage 2 §2.2 step 3, [V2.63] Q4) -----------------------------------


def test_the_same_deciding_facts_are_no_change() -> None:
    committed = deciding_facts(TYPE_ENTER, GOAL, read())
    assert site_check(committed, deciding_facts(TYPE_ENTER, GOAL, read())) == "same"


def test_a_new_text_elsewhere_on_the_page_is_no_change() -> None:
    """Never a whole-page hash: only what the outcome was computed from."""
    committed = deciding_facts(TYPE_ENTER, GOAL, read())
    more = dataclasses.replace(
        read(), snapshot=dataclasses.replace(read().snapshot, title="Shop, new banner")
    )
    assert site_check(committed, deciding_facts(TYPE_ENTER, GOAL, more)) == "same"


@pytest.mark.parametrize(
    "now", [{"searchbox": False}, {"landmark": False}, {"searchbox": False, "landmark": False}]
)
def test_a_changed_role_or_landmark_is_a_change(now: dict[str, bool]) -> None:
    committed = deciding_facts(TYPE_ENTER, GOAL, read())
    assert site_check(committed, deciding_facts(TYPE_ENTER, GOAL, read(**now))) == "changed"


# ---- the acts as data -------------------------------------------------------------------------


def test_the_acts_load_from_their_json() -> None:
    text = json.dumps(
        {
            "t1": {
                "acts": [
                    {"verb": "type", "role": "Edit", "name": "Find", "param": "red shoes"},
                    {"verb": "press", "key": "Enter"},
                ],
                "proof": "navigated",
                "why": "the results page's URL carries the words",
            }
        }
    )
    assert load_acts(text) == {"t1": TYPE_ENTER}


@pytest.mark.parametrize(
    "bad",
    [
        {
            "t1": {
                "acts": [{"verb": "fly", "role": "Edit", "name": "Find"}],
                "proof": None,
                "why": "w",
            }
        },
        {"t1": {"acts": [{"verb": "press", "key": "Home2"}], "proof": None, "why": "w"}},
        {"t1": {"acts": [], "proof": None, "why": "w"}},
        {
            "t1": {
                "acts": [{"verb": "click", "role": "Button", "name": "Go"}],
                "proof": "x",
                "why": "w",
            }
        },
        {
            "t1": {
                "acts": [{"verb": "click", "role": "Button", "name": "Go", "extra": 1}],
                "proof": None,
                "why": "w",
            }
        },
        {"t1": {"acts": [{"verb": "click", "role": "Button", "name": "Go"}], "proof": None}},
    ],
)
def test_acts_that_are_not_valid_are_refused(bad: object) -> None:
    with pytest.raises(ValueError, match="acts"):
        load_acts(json.dumps(bad))


# ---- the tasks: the pooled set's real-site rows, from the task data ------------------------------


def test_the_real_site_tasks_are_the_pooled_sets_thirty() -> None:
    tasks = real_site_tasks()
    assert len(tasks) == 30
    assert len({t.task_id for t in tasks}) == 30
    assert all(t.start.startswith("https://") for t in tasks)


@pytest.mark.probe
def test_save_reads_writes_one_read_per_real_site_task(tmp_path: Path) -> None:
    """The `--save-reads` pass (live, read-only, run in a slot): every real-site task gets its
    start page's full read, kept out of the repo."""
    from steerai.loop2.stage2_expected import main  # noqa: PLC0415 - the live pass

    assert main(["--save-reads", str(tmp_path)]) == 0
    for task in real_site_tasks():
        observed_from_json((tmp_path / f"{task.task_id}.json").read_text(encoding="utf-8"))


# ---- the start document's status (stage 2 §2.2 step 3; unmeasured until the save-reads slot) ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"uri": "https://a.test/", "title": "A", "status": 200}', (200, False, "A", True)),
        ('{"uri": "https://a.test/", "title": "A", "status": 302}', (302, False, "A", True)),
        ('{"uri": "https://a.test/", "title": "A", "status": 404}', (404, False, "A", False)),
        ('{"uri": "https://a.test/", "title": "A", "status": 0}', (None, False, "A", False)),
        (
            '{"uri": "chrome-error://chromewebdata/", "title": "", "status": 200}',
            (200, True, "", False),
        ),
        ('{"uri": "https://a.test/", "title": "A"}', (None, False, "A", False)),
        ("not json", (None, False, "", False)),
    ],
)
def test_the_start_document_is_loaded_only_with_a_2xx_or_3xx_and_no_error_page(
    raw: str, expected: tuple[int | None, bool, str, bool]
) -> None:
    """A status that is unread (0, missing, not a number) is never loaded: fail closed."""
    got = start_document(raw)
    assert (got.status, got.error_page, got.title, got.loaded) == expected
    assert isinstance(got, StartDocument)


# ---- #401 principal notes ---------------------------------------------------------------------


def _good_table() -> Any:  # Any: a JSON table, spoiled by each test
    return json.loads(dump_table([table_row("t1", TYPE_CLICK, GOAL, saved(read()))]))


@pytest.mark.parametrize(
    "spoil",
    [
        lambda t: t["t1"].update(extra=1),
        lambda t: t["t1"].update(read_sha256="not hex"),
        lambda t: t["t1"]["facts"].update(risk="R9"),
        lambda t: t["t1"]["facts"].update(colour="red"),
        lambda t: t["t1"]["facts"].pop("aria_role"),
        lambda t: t["t1"]["facts"].update(search_landmark="yes"),
        lambda t: t["t1"]["expected"].update(outcome="maybe"),
        lambda t: t["t1"]["expected"].update(proof="vibes"),
        lambda t: t["t1"]["expected"].update(task_id="t2"),
        lambda t: t["t1"]["expected"].update(reason=3),
    ],
)
def test_a_table_that_is_not_valid_is_refused(spoil: Any) -> None:  # Any: a test's mutation
    """As strict as `load_acts`: a `ValueError` naming `table`, never a partial row."""
    table = _good_table()
    spoil(table)
    with pytest.raises(ValueError, match="table"):
        load_table(json.dumps(table))


def test_an_unread_aria_role_or_landmark_map_is_unread_never_a_change() -> None:
    committed = deciding_facts(TYPE_ENTER, GOAL, read())
    no_roles = deciding_facts(TYPE_ENTER, GOAL, dataclasses.replace(read(), aria_roles=None))
    no_marks = deciding_facts(TYPE_ENTER, GOAL, dataclasses.replace(read(), landmarks=None))
    assert no_roles.unread == ("aria_role",)
    assert no_marks.unread == ("landmarks",)
    assert site_check(committed, no_roles) == "unread"
    assert site_check(committed, no_marks) == "unread"
    assert site_check(committed, deciding_facts(TYPE_ENTER, GOAL, read())) == "same"
    assert (
        site_check(committed, deciding_facts(TYPE_ENTER, GOAL, read(landmark=False))) == "changed"
    )


def test_a_table_row_is_never_built_from_a_read_with_unread_facts() -> None:
    with pytest.raises(ValueError, match="unread"):
        table_row("t1", TYPE_ENTER, GOAL, saved(dataclasses.replace(read(), aria_roles=None)))


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def test_loads_of_one_host_are_spaced_and_counted() -> None:
    """The save-reads pass and the harness space every load of a host alike (§2.3)."""
    clock = Clock()
    spacer = Spacer(clock, clock.sleep)
    for host in ("a.test", "b.test", "a.test"):
        spacer.wait(host)
        spacer.loaded(host)
    assert clock.slept == [pytest.approx(SITE_SPACING_S)]
    assert spacer.loads == {"a.test": 2, "b.test": 1}
