"""design-revise: the user's alterations replace the design text only before approval."""

import hashlib

import pytest
from flow import approve_direction
from test_tasks import FEATURE, task

from ticket_pipeline.lib import ledger, state_store
from ticket_pipeline.lib.task_store import operate


def ref(text):
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def draft(tmp_path, monkeypatch):
    """A freshly initialized feature: root issue only, direction not approved."""
    monkeypatch.setattr(state_store, "SCAFFOLD_DIR", tmp_path / ".scaffold")
    monkeypatch.setattr(state_store, "STATE_LOCK_FILE", tmp_path / ".scaffold/.tasks.lock")
    v1 = "design v1"
    operate("init", "F1", {**FEATURE, "context": v1, "design_ref": ref(v1)})
    return ledger.open_current("F1").path


def feature():
    return operate("list", "F1")["feature"]


def revise(text, design_ref=None):
    return operate(
        "design-revise", "F1", {"context": text, "design_ref": design_ref or ref(text)}
    )


def test_revision_replaces_design_and_reference_everywhere(draft):
    scope = feature()["direction_scope"]
    result = revise("design v2")["feature"]
    assert result["context"] == "design v2" and result["design_ref"] == ref("design v2")
    root = result["tasks"]["F1"]
    assert root["context"] == "design v2"
    assert root["origin"] == {"kind": "ticket", "reference": ref("design v2")}
    assert result["direction_scope"] != scope
    assert result["workflow"]["history"][-1]["event"] == "design-revised"


def test_revisions_can_repeat_and_approval_binds_the_latest(draft):
    revise("design v2")
    revise("design v3")
    assert feature()["design_ref"] == ref("design v3")
    approve_direction()
    assert feature()["direction"]["scope_digest"] == feature()["direction_scope"]


def test_approval_of_an_earlier_revision_is_rejected(draft):
    old = feature()["direction_scope"]
    revise("design v2")
    with pytest.raises(ValueError, match="does not match"):
        operate("direction-approve", "F1", {"scope_digest": old})


def test_revision_is_refused_after_approval(draft):
    approve_direction()
    before = draft.read_bytes()
    with pytest.raises(ValueError, match="already approved"):
        revise("design v2")
    assert draft.read_bytes() == before


def test_revision_is_refused_once_issue_designs_exist(draft):
    operate("decompose", "F1", [task("a")], "F1")
    before = draft.read_bytes()
    with pytest.raises(ValueError, match="before any issue design work"):
        revise("design v2")
    assert draft.read_bytes() == before


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"context": "x"},
        {"context": " ", "design_ref": "sha256:" + "0" * 64},
        {"context": "x", "design_ref": ref("x"), "extra": 1},
        {"context": 3, "design_ref": ref("x")},
    ],
)
def test_malformed_revisions_are_rejected(draft, payload):
    before = draft.read_bytes()
    with pytest.raises(ValueError):
        operate("design-revise", "F1", payload)
    assert draft.read_bytes() == before


def test_mismatched_design_ref_is_rejected(draft):
    before = draft.read_bytes()
    with pytest.raises(ValueError, match="does not match the revised context"):
        revise("design v2", design_ref=ref("something else"))
    assert draft.read_bytes() == before
