"""Where a physical click aimed, as a closed token (V2 step 1 packet §C; hunter OB-26; principal
re-review note): `aim` on every physical click, recording the point actually used. `label` is
the element's own label Text ([v2.81] `click_point`); `no_label` is a labelled role whose label
was not found (H6 u3's open question); `item` is the element itself. Never a name or a box."""

from __future__ import annotations

from steerai.errors import ErrorCode
from steerai.orchestrator.contracts import Step, Target
from tests.orchestrator import test_click_fallback as fallback
from tests.orchestrator import test_tree_expand_loop as tree
from tests.orchestrator.fakes import Row, not_delivered
from tests.orchestrator.test_loop import Harness, _type

PROJECTS_LABEL: Row = (35, "Text", "Projects", (42, 94), tree.PROJECTS[0])


def test_a_tree_item_with_no_label_text_is_no_label() -> None:
    h, _boxes = tree._harness()
    result = h.session.run_step(tree._expand())
    assert result.ok is True, result.to_dict()
    assert result.evidence["aim"] == "no_label"


def test_a_tree_item_clicked_on_its_label_is_label() -> None:
    h, _boxes = tree._harness()
    h.world.rows.append(PROJECTS_LABEL)

    def toggles(_executor: object, action: object) -> None:
        if getattr(action, "element_id", None) == PROJECTS_LABEL[0]:
            _boxes.expanded[tree.PROJECTS[3]] = 1

    h.physical.on_execute = toggles
    result = h.session.run_step(tree._expand())
    assert h.physical.calls == [("click", PROJECTS_LABEL[0], None)]
    assert result.evidence["aim"] == "label"


def test_a_button_without_invoke_clicked_physically_is_item() -> None:
    h = fallback._harness()
    fallback._no_invoke(h)
    assert h.session.run_step(fallback._click()).evidence["aim"] == "item"


def test_a_step_on_the_physical_path_is_item() -> None:
    h = fallback._harness()
    result = h.session.run_step(fallback._click(path="physical"))
    assert h.physical.calls == [("click", fallback.OPTIONS_ID, None)]
    assert result.evidence["aim"] == "item"


def test_no_physical_click_carries_no_aim() -> None:
    h = fallback._harness()
    assert "aim" not in h.session.run_step(fallback._click()).evidence, "a semantic Invoke"
    assert "aim" not in Harness().session.run_step(_type()).evidence, "typing is no click"


def test_a_refused_physical_click_still_says_where_it_would_have_aimed() -> None:
    h = fallback._harness()
    h.physical.outcomes[fallback.OPTIONS_ID] = not_delivered(
        ErrorCode.ELEMENT_NOT_ACTIONABLE, "clipped"
    )
    step: Step = fallback._click(path="physical", target=Target(role="Button", name="Chat options"))
    assert h.session.run_step(step).evidence["aim"] == "item"
