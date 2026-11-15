"""Stage 2's expected outcomes (packet v2-ports-adapter, stage 2 plan rev 4.4 §1.2): computed by
stage 1's `rate` and `outcome_of` from each task's saved start-page read, with the acts as data.
Only each read's SHA-256 and its deciding facts are committed; the reads stay local."""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from steerai.loop2.stage1_expected import Act, Expected
from steerai.loop2.stage2_expected import (
    ACTS,
    SITE_SPACING_S,
    TABLE,
    UNRATED_REASONS,
    DayDidNotTurn,
    DecidingFacts,
    SiteRow,
    Spacer,
    StartDocument,
    calendar_day,
    deciding_facts,
    dump_table,
    load_acts,
    load_table,
    load_unrated,
    real_site_tasks,
    site_check,
    start_document,
    table_row,
)
from steerai.world.effects import Observed
from steerai.world.elements import Element
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


# ---- the coordinator's rulings on the save-reads pass (2026-11-15) -----------------------------

BOX_TWIN = element(45, "Edit", "Find", (9, 45), 40)
"""A second copy of the field in the same search form: the same deciding facts."""
BOX_ELSEWHERE = element(46, "Edit", "Find", (9, 46), 1)
"""A copy outside the search form: another landmark, so other deciding facts."""


def twin_read(*extra: Element) -> Observed:
    base = read()
    elements = [*SHOWN, *extra]
    marks = dict(base.landmarks or {})
    search = Landmark(kind="search", runtime_id=FORM.runtime_id)
    for e in extra:
        marks[e.runtime_id] = search if e.parent_id == FORM.id else None
    roles = {**(base.aria_roles or {}), **{e.runtime_id: "searchbox" for e in extra}}
    return dataclasses.replace(
        base, snapshot=dataclasses.replace(base.snapshot, elements=elements),
        landmarks=marks, aria_roles=roles,
    )  # fmt: skip


def test_copies_of_a_target_that_agree_on_every_deciding_fact_are_rated() -> None:
    """The rating does not depend on which copy Steer picks, so it is not ambiguous."""
    assert deciding_facts(TYPE_CLICK, GOAL, twin_read(BOX_TWIN)) == deciding_facts(
        TYPE_CLICK, GOAL, read()
    )


def test_copies_that_disagree_are_ambiguous_naming_the_facts_that_differ() -> None:
    with pytest.raises(ValueError, match="ambiguous_target") as refused:
        deciding_facts(TYPE_CLICK, GOAL, twin_read(BOX_ELSEWHERE))
    assert "search_landmark" in str(refused.value)


def test_an_unrated_task_is_kept_out_of_the_table_with_its_reason() -> None:
    rows = [table_row("t1", TYPE_ENTER, GOAL, saved(read()))]
    text = dump_table(rows, unrated={"t9": "target_after_opener"})
    assert set(load_table(text)) == {"t1"}
    assert load_unrated(text) == {"t9": "target_after_opener"}


def test_the_acts_file_carries_the_unrated_tasks() -> None:
    text = json.dumps({"t1": {"acts": [{"verb": "press", "key": "Enter"}], "proof": None,
                              "why": "w"}, "t9": {"unrated": "pressed_unread"}})  # fmt: skip
    assert set(load_acts(text)) == {"t1"}
    assert load_unrated(text) == {"t9": "pressed_unread"}


@pytest.mark.parametrize("bad", [{"unrated": "because"}, {"unrated": "pressed_unread", "x": 1}])
def test_an_unrated_marker_that_is_not_valid_is_refused(bad: object) -> None:
    with pytest.raises(ValueError, match="unrated"):
        load_unrated(json.dumps({"t9": bad}))


def test_the_unrated_reasons_are_closed() -> None:
    assert (
        frozenset({"target_after_opener", "pressed_unread", "copies_disagree"}) == UNRATED_REASONS
    )


# ---- the coordinator's ruling on py1: a click pairs with the field it would submit ---------------

FORM_TWO = element(60, "Group", "", (9, 60), 1)
BOX_TWO = element(61, "Edit", "Find", (9, 61), 60)
GO_TWO = element(62, "Button", "Look up", (9, 62), 60)


def two_forms() -> Observed:
    """The page holds the search form twice: each field's button is in its own form."""
    base = read()
    marks = dict(base.landmarks or {})
    second = Landmark(kind="search", runtime_id=FORM_TWO.runtime_id)
    marks |= {FORM_TWO.runtime_id: None, BOX_TWO.runtime_id: second, GO_TWO.runtime_id: second}
    roles = {**(base.aria_roles or {}), BOX_TWO.runtime_id: "searchbox"}
    elements = [*SHOWN, FORM_TWO, BOX_TWO, GO_TWO]
    return dataclasses.replace(
        base, snapshot=dataclasses.replace(base.snapshot, elements=elements),
        landmarks=marks, aria_roles=roles,
    )  # fmt: skip


def test_a_click_is_rated_only_with_the_fields_in_its_submit_scope() -> None:
    """Typing into one form and clicking the other form's button is no submit of the text, and
    not what Steer does: those pairs are left out, as the gate's own [V2.22] scope decides."""
    assert deciding_facts(TYPE_CLICK, GOAL, two_forms()) == deciding_facts(TYPE_CLICK, GOAL, read())


def test_a_click_beside_no_copy_of_the_field_is_still_rated_on_every_pair() -> None:
    """When no pair is in scope the click submits nothing: every pair stands, as before."""
    lone = SiteRow(
        acts=(Act("type", "Edit", "Notes", param="red shoes"), Act("click", "Button", "Look up")),
        proof=None,
        why="w",
    )
    facts = deciding_facts(lone, GOAL, two_forms_notes())
    assert facts.button_role == "Button"


def two_forms_notes() -> Observed:
    """NOTE (the field) and both forms' buttons: NOTE is under the Document, so both buttons are
    under its parent container: both pairs are in scope, and agree."""
    return two_forms()


# ---- the committed 2b table ([V2.65], the save-reads and opener slots, 2026-11-15) --------------


def test_the_committed_2b_acts_and_table_cover_every_real_site_task() -> None:
    ids = {t.task_id for t in real_site_tasks()}
    acts = ACTS.read_text(encoding="utf-8")
    table = TABLE.read_text(encoding="utf-8")
    assert set(load_acts(acts)) | set(load_unrated(acts)) == ids
    assert set(load_table(table)) == set(load_acts(acts))
    assert load_unrated(table) == load_unrated(acts)


def test_the_committed_2b_table_holds_no_page_text() -> None:
    """Only digests, closed facts and the rows' own reasons: never a field's or page's name."""
    table = TABLE.read_text(encoding="utf-8")
    for row in load_table(table).values():
        assert len(row.read_sha256) == 64
    assert "Search Wikipedia" not in table


# ---- #413 blocker 1: the day turns in production, and the wait for it is bounded ----------------


def test_a_spacer_counts_days_on_the_calendar_by_default() -> None:
    clock = Clock()
    spacer = Spacer(clock, clock.sleep)
    assert spacer.day is calendar_day
    assert calendar_day() == datetime.date.today().toordinal()  # noqa: DTZ011 - the local day


def test_the_wait_for_the_day_to_turn_sleeps_an_hour_at_most_each_time() -> None:
    clock = Clock()

    def days() -> int:  # the fake calendar
        return int(clock() // 86_400)

    clock.now = 86_400 * 5 + 100.0
    spacer = Spacer(clock, clock.sleep, day=days)
    spacer.wait_next_day()
    assert days() == 6
    assert max(clock.slept) <= 3600.0


def test_a_day_that_never_turns_is_refused_after_a_bounded_wait() -> None:
    clock = Clock()
    spacer = Spacer(clock, clock.sleep, day=lambda: 7)
    with pytest.raises(DayDidNotTurn, match="day_did_not_turn"):
        spacer.wait_next_day()
    assert len(clock.slept) <= 27
    assert sum(clock.slept) <= 27 * 3600.0
