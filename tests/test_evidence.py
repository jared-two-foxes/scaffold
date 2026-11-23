"""Requirement evidence: structural association with attempts, checkpoints and validation."""

import copy

import pytest
from flow import (
    approve_direction,
    record_attempt,
    record_checkpoint,
    record_evidence,
    reference_for,
    sha,
)
from test_tasks import FEATURE, approve_all, evidence, task

from ticket_pipeline.lib import coverage, ledger, state_store
from ticket_pipeline.lib import evidence as requirement_evidence
from ticket_pipeline.lib.task_store import operate

BOTH = ("contribute", "verify")


def linked(identifier, roles, deps=()):
    base = {k: v for k, v in task(identifier, deps).items() if k != "requirements_rationale"}
    return {**base, "requirements": [{"id": "REQ-1", "roles": list(roles)}]}


@pytest.fixture
def multi(tmp_path, monkeypatch):
    """REQ-1 needs two contributors (a, b) and a verifier (v) ordered after both."""
    monkeypatch.setattr(state_store, "SCAFFOLD_DIR", tmp_path / ".scaffold")
    monkeypatch.setattr(state_store, "STATE_LOCK_FILE", tmp_path / ".scaffold/.tasks.lock")
    operate("init", "F1", FEATURE)
    operate(
        "decompose",
        "F1",
        [
            linked("a", ["contribute"]),
            linked("b", ["contribute"], ["a"]),
            linked("v", ["verify"], ["a", "b"]),
        ],
        "F1",
    )
    approve_all()
    approve_direction()


def feature():
    return operate("list", "F1")["feature"]


def status():
    return operate("requirements", "F1")["requirements"][0]["evidence"]


def payload(task_id, role, receipt=None, **override):
    current = feature()
    task_state = current["tasks"][task_id]
    plan = coverage.compute(current)
    receipt = receipt or task_state["attempts"][-1]["receipt"]
    attempt = next(a for a in task_state["attempts"] if a["receipt"] == receipt)
    base = {
        "feature_id": "F1",
        "instance_id": current["workflow"]["instance_id"],
        "requirement": "REQ-1",
        "role": role,
        "result": "PASSED",
        "basis": "checks",
        "receipt": receipt,
        "tree": attempt["tree"],
        "binding": requirement_evidence.binding(plan, "REQ-1"),
        "design": task_state["designs"][-1]["reference"],
        "reference": operate("artifact-put", "F1", {"assessment": task_id, "receipt": receipt})["reference"],
    }
    return {**base, **override}


def work(task_id, basis="checks", checkpoint=True):
    operate("start", "F1", task_id=task_id)
    reference = record_attempt("F1", task_id)
    operate("complete", "F1", evidence(task_id), task_id)
    if checkpoint:
        record_checkpoint("F1", task_id)
    return reference


def test_valid_evidence_survives_replay_and_is_retrievable(multi, tmp_path):
    operate("start", "F1", task_id="a")
    reference = record_attempt("F1", "a")
    stored = feature()["evidence"][0]
    assert stored["task_id"] == "a" and stored["receipt"] == reference
    assert operate("artifact-get", "F1", {"reference": stored["reference"]})["artifact"]
    before = ledger.open_current("F1").path.read_bytes()
    operate("evidence-record", "F1", payload("a", "contribute"), "a")  # idempotent replay
    assert ledger.open_current("F1").path.read_bytes() == before
    replayed = ledger.Ledger(ledger.open_current("F1").directory)
    assert replayed.state["evidence"] == feature()["evidence"]


def test_multi_task_requirement_is_incomplete_until_every_contribution_and_verification(multi):
    assert status()["status"] == "unresolved"
    work("a")
    assert status()["implementation"] == "unresolved" and status()["status"] == "unresolved"
    work("b")
    assert status()["implementation"] == "satisfied"
    assert status()["verification"] == "unresolved"
    with pytest.raises(ValueError, match="evidence incomplete"):
        requirement_evidence.require_satisfied(feature())
    work("v")
    assert status()["status"] == "satisfied"
    assert [o["status"] for o in status()["obligations"]] == ["satisfied"] * 3
    requirement_evidence.require_satisfied(feature())


def test_completion_needs_evidence_for_every_assigned_role(multi):
    operate("start", "F1", task_id="a")
    reference = operate("artifact-put", "F1", {"receipt": "a"})["reference"]
    feature_state = feature()
    operate(
        "attempt-record",
        "F1",
        {
            "attempt": 1,
            "status": "PASSED",
            "receipt": reference,
            "base": feature_state["head"],
            "tree": sha("tree-a"),
        },
        "a",
    )
    with pytest.raises(ValueError, match="Completion needs evidence"):
        operate("complete", "F1", evidence("a"), "a")
    record_evidence("F1", "a", reference)
    operate("complete", "F1", evidence("a"), "a")


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda p: {"requirement": "REQ-9"}, "Unknown requirement"),
        (lambda p: {"role": "verify"}, "not a required verify"),
        (lambda p: {"role": "review"}, "Unknown evidence role"),
        (lambda p: {"feature_id": "OTHER"}, "different feature instance"),
        (lambda p: {"instance_id": "0" * 32}, "different feature instance"),
        (lambda p: {"result": "FAILED"}, "result PASSED"),
        (lambda p: {"basis": "trust-me"}, "result PASSED"),
        (lambda p: {"tree": sha("other-tree")}, "tree does not match"),
        (lambda p: {"receipt": "sha256:" + "1" * 64}, "Artifact is missing"),
        (lambda p: {"binding": "0" * 64}, "different design"),
        (lambda p: {"design": "sha256:" + "2" * 64}, "different design"),
        (lambda p: {"tree": "nope"}, "full tree SHA"),
    ],
)
def test_unknown_mismatched_or_foreign_evidence_is_rejected(multi, change, message):
    operate("start", "F1", task_id="a")
    record_attempt("F1", "a")
    before = ledger.open_current("F1").path.read_bytes()
    base = payload("a", "contribute")
    with pytest.raises(ValueError, match=message):
        operate("evidence-record", "F1", {**base, **change(base)}, "a")
    with pytest.raises(ValueError):
        operate("evidence-record", "F1", {k: v for k, v in base.items() if k != "tree"}, "a")
    assert ledger.open_current("F1").path.read_bytes() == before


def test_cross_task_and_unstarted_evidence_cannot_satisfy_another_task(multi):
    work("a")
    operate("start", "F1", task_id="b")
    record_attempt("F1", "b")
    # a's receipt cannot be claimed for b, and the unstarted verifier has no attempts at all.
    borrowed = payload("a", "contribute")
    with pytest.raises(ValueError, match="different design|recorded PASSED attempt"):
        operate("evidence-record", "F1", borrowed, "b")
    with pytest.raises(ValueError, match="different design|not a required|started work"):
        operate("evidence-record", "F1", {**borrowed, "role": "verify"}, "v")
    assert len(feature()["evidence"]) == 3 - 1  # only a's and b's own associations


def test_failed_attempt_and_superseded_pass_are_rejected_and_stay_unresolved(multi):
    operate("start", "F1", task_id="a")
    first = record_attempt("F1", "a")
    second = record_attempt("F1", "a")  # a newer passing attempt supersedes the first
    with pytest.raises(ValueError, match="superseded"):
        operate("evidence-record", "F1", payload("a", "contribute", receipt=first), "a")
    # The first attempt's own association (recorded when it was current) is now stale history.
    stale = [e for e in feature()["evidence"] if e["receipt"] == first]
    assert stale and status()["obligations"][0]["status"] == "unresolved"
    with pytest.raises(ValueError, match="Completion needs evidence"):
        operate("complete", "F1", {**evidence("a"), "reference": first}, "a")
    operate("complete", "F1", {**evidence("a"), "reference": second}, "a")
    assert status()["obligations"][0]["records"] == 2


def test_failed_attempt_never_counts(multi):
    operate("start", "F1", task_id="a")
    reference = record_attempt("F1", "a", status="BLOCKED")
    with pytest.raises(ValueError, match="PASSED attempt"):
        operate("evidence-record", "F1", payload("a", "contribute", receipt=reference), "a")


def test_duplicate_association_must_match_and_unstored_artifacts_are_refused(multi):
    operate("start", "F1", task_id="a")
    record_attempt("F1", "a")
    different = payload("a", "contribute")
    different["basis"] = "independent-review"
    with pytest.raises(ValueError, match="already recorded differently"):
        operate("evidence-record", "F1", different, "a")
    with pytest.raises(ValueError, match="missing"):
        operate(
            "evidence-record", "F1", payload("a", "contribute", reference="sha256:" + "3" * 64), "a"
        )
    assert len(feature()["evidence"]) == 1


def test_checkpoint_tree_must_be_the_completing_attempts_tree(multi):
    work("a", checkpoint=False)
    current = feature()
    with pytest.raises(ValueError, match="tree of the attempt"):
        operate(
            "checkpoint-record",
            "F1",
            {
                "commit": sha("c-a"),
                "parent": current["head"],
                "tree": sha("wrong"),
                "reference": reference_for("a"),
            },
            "a",
        )
    assert "checkpoint" not in feature()["tasks"]["a"]


def test_final_validation_refuses_incomplete_coverage_and_checks_only_is_not_independent(multi):
    work("a")
    work("b")
    work("v")
    work("F1")
    assert status()["independent_verification"] is False  # checks alone are not independent review
    corrupt = copy.deepcopy(ledger.open_current("F1").state)
    corrupt["evidence"] = [e for e in corrupt["evidence"] if e["task_id"] != "v"]
    ledger.open_current("F1").append("test-drop", {}, corrupt)
    assert status()["verification"] == "missing"
    with pytest.raises(ValueError, match="evidence incomplete"):
        start_validation()


def start_validation():
    from test_feature_validation import start

    start()


def test_independent_review_basis_is_reported_for_verification(multi):
    work("a")
    work("b")
    operate("start", "F1", task_id="v")
    receipt = record_attempt("F1", "v", status="PASSED")
    # Replace the checks-based association by recording with the independent-review basis.
    corrupt = copy.deepcopy(ledger.open_current("F1").state)
    for entry in corrupt["evidence"]:
        if entry["task_id"] == "v":
            entry["basis"] = "independent-review"
    ledger.open_current("F1").append("test-basis", {}, corrupt)
    operate("complete", "F1", {**evidence("v"), "reference": receipt}, "v")
    record_checkpoint("F1", "v")
    assert status()["independent_verification"] is True


def test_design_reopening_keeps_history_but_changed_binding_makes_it_stale(multi):
    work("a")
    work("b")
    work("v")
    kept = copy.deepcopy(feature()["evidence"])
    operate("design-reopen", "F1", {"reason": "Add another contributor"})
    operate("add", "F1", [{**linked("extra", BOTH), "dependencies": []}])
    approve_all()
    after = feature()
    assert after["evidence"] == kept  # nothing is dropped
    # The new contributor changes the requirement's contributors/verifiers: nothing applies.
    assert status()["status"] == "stale"
    with pytest.raises(ValueError, match="evidence incomplete"):
        requirement_evidence.require_satisfied(after)


def test_unrelated_graph_changes_do_not_invalidate_evidence(multi):
    work("a")
    work("b")
    work("v")
    operate("design-reopen", "F1", {"reason": "Add supporting work"})
    operate("add", "F1", [task("support")])
    approve_all()
    assert status()["status"] == "satisfied"


def test_history_rewrite_makes_evidence_stale_until_authenticated_equivalence(multi):
    from test_ledger_rules import carry_over, rewrite_payload

    for task_id in ("a", "b", "v"):
        work(task_id)
    operate("history-rewrite", "F1", rewrite_payload(feature()))
    assert status()["status"] == "stale"
    with pytest.raises(ValueError, match="evidence incomplete"):
        requirement_evidence.require_satisfied(feature())
    entry = feature()["evidence"][0]
    task_tree = feature()["tasks"][entry["task_id"]]["checkpoint"]["tree"]
    proof = operate("artifact-put", "F1", {"rewrite": 1})["reference"]
    with pytest.raises(ValueError, match="current checkpoint tree"):
        operate(
            "evidence-equivalence",
            "F1",
            {
                "requirement": "REQ-1",
                "role": entry["role"],
                "receipt": entry["receipt"],
                "tree": sha("elsewhere"),
                "reference": proof,
            },
            entry["task_id"],
        )
    carry_over(feature())
    assert status()["status"] == "satisfied"
    assert len(feature()["evidence"]) == 3  # history retained, equivalence only annotated
    assert task_tree != entry["tree"]
