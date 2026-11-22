"""design-revise: the user's alterations replace the design text only before approval."""

import hashlib

import pytest
from flow import approve_direction
from test_tasks import FEATURE, task

from ticket_pipeline.lib import ledger, requirements, state_store
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


def test_revision_is_refused_after_a_recorded_design_is_reopened(draft):
    """Reopening moves the record to design_history; the earlier design work still counts."""
    operate(
        "design-record",
        "F1",
        {"summary": "s", "verification": "v", "approval": "a", "disposition": "leaf"},
        "F1",
    )
    operate("design-reopen", "F1", {"reason": "Rethink"}, "F1")
    root = feature()["tasks"]["F1"]
    assert not root.get("designs") and root.get("design_history")
    before = draft.read_bytes()
    with pytest.raises(ValueError, match="before any issue design work"):
        revise("design v2")
    assert draft.read_bytes() == before


def test_revision_replaces_requirements_with_the_context(draft):
    """Title, objective and constraints are replaced with the context; requirements are not."""
    result = operate(
        "design-revise",
        "F1",
        {
            "context": "design v2",
            "design_ref": ref("design v2"),
            "title": "New title",
            "objective": "New objective",
            "constraints": ["No more"],
        },
    )["feature"]
    root = result["tasks"]["F1"]
    for target in (result, root):
        assert target["title"] == "New title"
        assert target["objective"] == "New objective"
        assert target["constraints"] == ["No more"]
    assert result["requirements"] == requirements.canonical(FEATURE["requirements"])
    assert root["acceptance_criteria"] == ["Works"]


def test_omitted_requirements_are_left_unchanged(draft):
    before = feature()
    result = revise("design v2")["feature"]
    for field in ("title", "objective", "requirements"):
        assert result[field] == before[field]
    for field in ("title", "objective", "acceptance_criteria"):
        assert result["tasks"]["F1"][field] == before["tasks"]["F1"][field]


def test_requirement_features_refuse_feature_criteria_revision(draft):
    before = draft.read_bytes()
    with pytest.raises(ValueError, match="replaced by requirements"):
        operate(
            "design-revise",
            "F1",
            {"context": "x", "design_ref": ref("x"), "acceptance_criteria": ["Only this"]},
        )
    assert draft.read_bytes() == before


@pytest.mark.parametrize(
    "extra",
    [
        {"acceptance_criteria": []},
        {"acceptance_criteria": [" "]},
        {"acceptance_criteria": "x"},
        {"objective": " "},
        {"title": 3},
        {"constraints": [1]},
        {"unknown": 1},
    ],
)
def test_invalid_requirement_revisions_leave_the_ledger_unchanged(draft, extra):
    before = draft.read_bytes()
    with pytest.raises(ValueError):
        operate(
            "design-revise",
            "F1",
            {"context": "design v2", "design_ref": ref("design v2"), **extra},
        )
    assert draft.read_bytes() == before


def reqs(*pairs):
    return [{"id": identifier, "text": text} for identifier, text in pairs]


def revise_requirements(items, text="design v2"):
    return operate(
        "design-revise",
        "F1",
        {"context": text, "design_ref": ref(text), "requirements": items},
    )["feature"]


def test_requirement_revision_replaces_adds_removes_and_preserves_ids(draft):
    added = revise_requirements(reqs(("REQ-1", "Works"), ("REQ-2", "New one")))
    assert [r["id"] for r in added["requirements"]] == ["REQ-1", "REQ-2"]
    assert added["tasks"]["F1"]["acceptance_criteria"] == ["Works", "New one"]
    assert "acceptance_criteria" not in added
    edited = revise_requirements(reqs(("REQ-1", "Works better")), "design v3")
    assert edited["requirements"] == [{"id": "REQ-1", "text": "Works better"}]
    assert edited["tasks"]["F1"]["acceptance_criteria"] == ["Works better"]
    assert edited["workflow"]["history"][-1]["removed_requirements"] == ["REQ-2"]


def test_a_removed_requirement_id_cannot_be_reused(draft):
    revise_requirements(reqs(("REQ-2", "Replacement")))
    before = draft.read_bytes()
    with pytest.raises(ValueError, match="removed earlier"):
        revise_requirements(reqs(("REQ-1", "Back again"), ("REQ-2", "Replacement")), "design v3")
    assert draft.read_bytes() == before


@pytest.mark.parametrize(
    "items",
    [
        [],
        None,
        "x",
        [{"id": "bad id", "text": "x"}],
        [{"id": "R", "text": ""}],
        [{"id": "R", "text": "a"}, {"id": "R", "text": "b"}],
        [{"id": "R", "text": "a", "waived": True}],
    ],
)
def test_malformed_requirement_revisions_leave_the_ledger_unchanged(draft, items):
    before = draft.read_bytes()
    with pytest.raises(ValueError):
        revise_requirements(items)
    assert draft.read_bytes() == before


def test_requirement_revision_alone_makes_an_earlier_approval_stale(draft):
    stale = feature()["direction_scope"]
    revise_requirements(reqs(("REQ-1", "Works"), ("REQ-2", "More")), "design v1")
    assert feature()["design_ref"] == ref("design v1")
    assert feature()["direction_scope"] != stale
    with pytest.raises(ValueError, match="does not match"):
        operate("direction-approve", "F1", {"scope_digest": stale})
    approve_direction()
    assert feature()["direction"]["scope_digest"] == feature()["direction_scope"]


def test_approved_requirements_are_frozen(draft):
    approve_direction()
    approved = feature()["requirements"]
    before = draft.read_bytes()
    with pytest.raises(ValueError, match="frozen"):
        revise_requirements(reqs(("REQ-1", "Changed")))
    with pytest.raises(ValueError, match="scope transition"):
        revise("design v2")
    assert draft.read_bytes() == before
    assert feature()["requirements"] == approved


def test_revised_requirements_replay_from_the_ledger(draft):
    revise_requirements(reqs(("REQ-2", "Two"), ("REQ-1", "One")))
    live = ledger.open_current("F1")
    replayed = ledger.Ledger(live.directory)
    assert replayed.state == live.state
    assert [r["id"] for r in replayed.state["requirements"]] == ["REQ-1", "REQ-2"]
