"""Requirements at init: canonical storage, root derivation, digest binding and legacy ledgers."""

import copy
import hashlib
import itertools

import pytest
from flow import approve_direction
from test_tasks import FEATURE

from ticket_pipeline.lib import (
    delivery,
    direction,
    issue_workflow,
    ledger,
    requirements,
    state_store,
)
from ticket_pipeline.lib.task_store import operate

REQS = [
    {"id": "REQ-2", "text": "second", "source": "SA-659"},
    {"id": "REQ-1", "text": "first"},
]


def use_state(monkeypatch, root):
    monkeypatch.setattr(state_store, "SCAFFOLD_DIR", root / ".scaffold")
    monkeypatch.setattr(state_store, "STATE_LOCK_FILE", root / ".scaffold/.tasks.lock")


def definition(**changes):
    return {**FEATURE, "requirements": copy.deepcopy(REQS), **changes}


def ref(text):
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def state(tmp_path, monkeypatch):
    use_state(monkeypatch, tmp_path)
    return tmp_path


def test_init_stores_canonical_requirements_and_derives_the_root_contract(state):
    feature = operate("init", "F1", definition())["feature"]
    assert feature["requirements"] == requirements.canonical(REQS)
    assert [r["id"] for r in feature["requirements"]] == ["REQ-1", "REQ-2"]
    assert "acceptance_criteria" not in feature
    assert feature["tasks"]["F1"]["acceptance_criteria"] == ["first", "second"]
    assert feature["workflow"]["kind"] == issue_workflow.KIND == "recursive-issues/v2"


@pytest.mark.parametrize(
    "change",
    [
        {"requirements": []},
        {"requirements": None},
        {"requirements": "x"},
        {"requirements": [{"id": "bad id", "text": "x"}]},
        {"requirements": [{"id": "R", "text": " "}]},
        {"requirements": [{"id": "R", "text": "a"}, {"id": "R", "text": "b"}]},
        {"requirements": [{"id": "R", "text": "a", "waived": True}]},
        {"acceptance_criteria": ["Works"]},
    ],
)
def test_malformed_requirements_are_rejected_before_any_ledger_write(state, change):
    with pytest.raises(ValueError):
        operate("init", "F1", definition(**change))
    assert not (state / ".scaffold/features").exists()


def test_legacy_shaped_init_is_rejected(state):
    legacy = {k: v for k, v in FEATURE.items() if k != "requirements"}
    with pytest.raises(ValueError, match="nonempty array"):
        operate("init", "F1", legacy)
    with pytest.raises(ValueError, match="replaced by requirements"):
        operate("init", "F1", {**legacy, "acceptance_criteria": ["Works"]})
    assert not (state / ".scaffold/features").exists()


def test_input_order_never_changes_stored_identity(tmp_path, monkeypatch):
    stored = []
    for number, order in enumerate(itertools.permutations(REQS)):
        use_state(monkeypatch, tmp_path / str(number))
        feature = operate("init", "F1", definition(requirements=list(order)))["feature"]
        stored.append(feature["requirements"])
    assert len(stored) == 2 and all(s == stored[0] for s in stored)


def test_digest_is_independent_of_requirement_order(state):
    operate("init", "F1", definition())
    feature = ledger.open_current("F1").state
    scope = direction.definition_digest(feature)
    for order in itertools.permutations(REQS):
        assert direction.definition_digest({**feature, "requirements": list(order)}) == scope


def test_approval_binds_the_exact_requirement_set_and_instance(state):
    operate("init", "F1", definition())
    feature = ledger.open_current("F1").state
    scope = direction.definition_digest(feature)
    edited = copy.deepcopy(feature["requirements"])
    edited[0]["text"] = "edited"
    added = [*feature["requirements"], {"id": "REQ-3", "text": "third"}]
    for variant in (edited, feature["requirements"][:1], added):
        assert direction.definition_digest({**feature, "requirements": variant}) != scope
    other = {**feature, "workflow": {**feature["workflow"], "instance_id": "b" * 32}}
    assert direction.definition_digest(other) != scope
    stale = direction.definition_digest({**feature, "requirements": edited})
    path = ledger.open_current("F1").path
    before = path.read_bytes()
    with pytest.raises(ValueError, match="does not match"):
        operate("direction-approve", "F1", {"scope_digest": stale})
    assert path.read_bytes() == before
    approve_direction("F1")
    assert operate("list", "F1")["feature"]["direction"]["scope_digest"] == scope


def test_ledger_replay_folds_to_identical_state(state):
    operate("init", "F1", definition())
    approve_direction("F1")
    live = ledger.open_current("F1")
    replayed = ledger.Ledger(live.directory)
    assert replayed.state == live.state == ledger.verify(replayed.entries)
    assert replayed.state["requirements"] == requirements.canonical(REQS)
    assert "acceptance_criteria" not in replayed.state


def test_delivery_contract_uses_the_derived_projection():
    event = {"kind": "initialized", "context": {}, "head": "a" * 40, "sequence": 0}
    payload = {"expected_revision": 0, "event": event}
    modern = {
        "objective": "o",
        "requirements": copy.deepcopy(REQS),
        "tasks": {"t": {"status": "in_progress"}},
    }
    legacy = {
        "objective": "o",
        "acceptance_criteria": ["stored"],
        "tasks": {"t": {"status": "in_progress"}},
    }
    delivery.record(modern, "t", payload)
    delivery.record(legacy, "t", payload)
    assert modern["tasks"]["t"]["delivery"]["contract"]["acceptance_criteria"] == [
        "first",
        "second",
    ]
    assert legacy["tasks"]["t"]["delivery"]["contract"]["acceptance_criteria"] == ["stored"]


def legacy_ledger(root, monkeypatch):
    """A ledger as the pre-requirements schema wrote it: acceptance_criteria, workflow v1."""
    use_state(monkeypatch, root / "modern")
    operate("init", "F1", definition())
    legacy = copy.deepcopy(ledger.open_current("F1").state)
    use_state(monkeypatch, root / "legacy")
    del legacy["requirements"]
    legacy["acceptance_criteria"] = ["Works"]
    legacy["workflow"]["kind"] = issue_workflow.LEGACY_KIND
    legacy["tasks"]["F1"]["acceptance_criteria"] = ["Works"]
    created = ledger.create("F1", legacy["workflow"]["instance_id"])
    created.append("init", {"task": None, "payload": {}}, legacy)
    return legacy, created


def test_legacy_ledgers_stay_readable_and_keep_their_original_digest(tmp_path, monkeypatch):
    legacy, created = legacy_ledger(tmp_path, monkeypatch)
    listed = operate("list", "F1")["feature"]
    assert listed["acceptance_criteria"] == ["Works"] and "requirements" not in listed
    assert listed["workflow"]["kind"] == "recursive-issues/v1"
    scope = {key: legacy.get(key) for key in direction.LEGACY_DEFINITION}
    scope["instance_id"] = legacy["workflow"]["instance_id"]
    assert listed["direction_scope"] == ledger.digest(scope)
    approve_direction("F1")
    approved = ledger.Ledger(created.directory).state
    assert approved["direction"]["scope_digest"] == listed["direction_scope"]
    assert "requirements" not in approved


def test_legacy_features_keep_their_revise_path_and_refuse_requirements(tmp_path, monkeypatch):
    _, created = legacy_ledger(tmp_path, monkeypatch)
    revised = operate(
        "design-revise",
        "F1",
        {"context": "v2", "design_ref": ref("v2"), "acceptance_criteria": ["Only this"]},
    )["feature"]
    assert revised["acceptance_criteria"] == ["Only this"]
    assert revised["tasks"]["F1"]["acceptance_criteria"] == ["Only this"]
    before = created.path.read_bytes()
    with pytest.raises(ValueError, match="legacy"):
        operate(
            "design-revise",
            "F1",
            {"context": "v3", "design_ref": ref("v3"), "requirements": copy.deepcopy(REQS)},
        )
    assert created.path.read_bytes() == before


def test_approved_legacy_features_refuse_every_revision_as_frozen(tmp_path, monkeypatch):
    _, created = legacy_ledger(tmp_path, monkeypatch)
    approve_direction("F1")
    before = created.path.read_bytes()
    for extra in ({}, {"acceptance_criteria": ["Only this"]}, {"requirements": copy.deepcopy(REQS)}):
        with pytest.raises(ValueError, match="frozen"):
            operate("design-revise", "F1", {"context": "v2", "design_ref": ref("v2"), **extra})
    assert created.path.read_bytes() == before
