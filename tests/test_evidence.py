"""Requirement evidence: structural association with attempts, checkpoints and validation."""

import copy
import json

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
    attempts = task_state.get("attempts", [])
    receipt = receipt or attempts[-1]["receipt"]
    tree = attempts[-1]["tree"] if attempts else sha("tree-" + task_id)
    base = {
        "feature_id": "F1",
        "instance_id": current["workflow"]["instance_id"],
        "requirement": "REQ-1",
        "role": role,
        "result": "PASSED",
        "basis": "checks",
        "receipt": receipt,
        "tree": tree,
        "binding": requirement_evidence.binding(plan, "REQ-1"),
        "design": task_state["designs"][-1]["reference"],
        "reference": operate(
            "artifact-put", "F1", {"assessment": task_id, "receipt": receipt, **override.pop("note", {})}
        )["reference"],
    }
    return {**base, **override}


def work(task_id, basis="checks", checkpoint=True):
    operate("start", "F1", task_id=task_id)
    reference = record_attempt("F1", task_id)
    operate("complete", "F1", evidence(task_id), task_id)
    if checkpoint:
        record_checkpoint("F1", task_id)
    return reference


def test_valid_evidence_survives_replay_and_is_retrievable(multi):
    operate("start", "F1", task_id="a")
    reference = record_attempt("F1", "a")
    path = ledger.open_current("F1").path
    before = path.read_bytes()
    operate("evidence-record", "F1", payload("a", "contribute"), "a")  # idempotent replay
    assert path.read_bytes() == before
    replayed = ledger.Ledger(path.parent)  # rebuilt from the chain alone
    assert replayed.state["evidence"] == feature()["evidence"]
    assert len(replayed.state["evidence"]) == 1
    stored = replayed.state["evidence"][0]
    plan = coverage.compute(replayed.state)
    assert {k: stored[k] for k in ("requirement", "task_id", "role", "result", "receipt", "tree")} == {
        "requirement": "REQ-1",
        "task_id": "a",
        "role": "contribute",
        "result": "PASSED",
        "receipt": reference,
        "tree": sha("tree-a"),
    }
    assert stored["binding"] == requirement_evidence.binding(plan, "REQ-1")
    blob = replayed.get_artifact(stored["reference"].removeprefix("sha256:"))
    assert json.loads(blob) == {"assessment": "a", "receipt": reference}
    assert operate("artifact-get", "F1", {"reference": stored["reference"]})["artifact"] == {
        "assessment": "a",
        "receipt": reference,
    }


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


def test_cross_task_and_non_receipt_evidence_cannot_satisfy_another_task(multi):
    a_receipt = work("a")
    operate("start", "F1", task_id="b")
    b_receipt = record_attempt("F1", "b")
    path = ledger.open_current("F1").path
    before = path.read_bytes()
    # Otherwise valid for b (its own design and binding), citing only a's stored receipt.
    borrowed = payload("b", "contribute", receipt=a_receipt, tree=sha("tree-a"))
    with pytest.raises(ValueError, match="recorded PASSED attempt of this task"):
        operate("evidence-record", "F1", borrowed, "b")
    # A stored artifact that is not an attempt receipt at all.
    not_a_receipt = operate("artifact-put", "F1", {"just": "a blob"})["reference"]
    with pytest.raises(ValueError, match="recorded PASSED attempt of this task"):
        operate("evidence-record", "F1", payload("b", "contribute", receipt=not_a_receipt), "b")
    assert path.read_bytes() == before
    b = next(o for o in status()["obligations"] if o["task_id"] == "b")
    assert b["status"] == "unresolved" and b["evidence"]["receipt"] == b_receipt


def test_unstarted_task_cannot_hold_evidence(multi):
    work("a")
    work("b")
    path = ledger.open_current("F1").path
    before = path.read_bytes()
    with pytest.raises(ValueError, match="started work"):
        operate("evidence-record", "F1", payload("v", "verify", receipt=reference_for("a")), "v")
    assert path.read_bytes() == before
    assert status()["verification"] == "unresolved"


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


def test_design_reopening_keeps_history_and_corrective_work_restores_coverage(multi):
    for task_id in ("a", "b", "v", "F1"):
        work(task_id)
    kept = copy.deepcopy(feature()["evidence"])
    operate("design-reopen", "F1", {"reason": "Add another contributor"})
    operate("add", "F1", [{**linked("extra", BOTH), "dependencies": []}])
    approve_all()
    assert feature()["evidence"] == kept  # nothing is dropped
    # The new contributor changes the requirement's binding: nothing recorded applies any more.
    assert status()["status"] == "stale"
    with pytest.raises(ValueError, match="evidence incomplete"):
        requirement_evidence.require_satisfied(feature())
    # Completed tasks cannot be reopened and keep their receipts: the same receipt is reassessed
    # under the new binding by a new, separately retained association (a fresh reference).
    work("extra")
    assert status()["status"] == "stale"  # still the earlier tasks' records
    for task_id, roles in (("a", ["contribute"]), ("b", ["contribute"]), ("v", ["verify"])):
        for role in roles:
            receipt = feature()["tasks"][task_id]["attempts"][-1]["receipt"]
            stale = payload(task_id, role)
            assert stale["receipt"] == receipt
            earlier = next(e for e in kept if e["task_id"] == task_id and e["role"] == role)
            replay = {**stale, "reference": earlier["reference"]}
            with pytest.raises(ValueError, match="own retained reference"):
                operate("evidence-record", "F1", replay, task_id)
            operate(
                "evidence-record",
                "F1",
                payload(task_id, role, note={"reassessed": "after corrective work"}),
                task_id,
            )
    assert status()["status"] == "satisfied"
    assert len(feature()["evidence"]) == len(kept) + 2 + 3  # extra's two roles, three reassessed
    assert all(e in feature()["evidence"] for e in kept)
    start_validation()
    assert feature()["validation"]["status"] == "validating"


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


# --- overall status derives from the implementation and verification roles --------------------


@pytest.fixture
def design_only(tmp_path, monkeypatch):
    monkeypatch.setattr(state_store, "SCAFFOLD_DIR", tmp_path / ".scaffold")
    monkeypatch.setattr(state_store, "STATE_LOCK_FILE", tmp_path / ".scaffold/.tasks.lock")
    operate("init", "F1", FEATURE)


def test_missing_verifier_is_reported_missing_not_unresolved(design_only):
    operate("decompose", "F1", [linked("a", ["contribute"])], "F1")
    item = status()
    assert item["implementation"] == "unresolved"
    assert item["verification"] == "missing"
    assert item["status"] == "missing"
    assert operate("summary", "F1")["summary"]["requirements"]["evidence"]["missing"] == 1


def test_missing_contributor_is_reported_missing_not_unresolved(design_only):
    operate("decompose", "F1", [linked("v", ["verify"])], "F1")
    item = status()
    assert item["implementation"] == "missing"
    assert item["verification"] == "unresolved"
    assert item["status"] == "missing"


# --- several verifiers, and a reviewer saying not_applicable ------------------------------------


@pytest.fixture
def multiverify(tmp_path, monkeypatch):
    """REQ-1 has one contributor (a) and two verifiers (v1, v2), both ordered after it."""
    monkeypatch.setattr(state_store, "SCAFFOLD_DIR", tmp_path / ".scaffold")
    monkeypatch.setattr(state_store, "STATE_LOCK_FILE", tmp_path / ".scaffold/.tasks.lock")
    operate("init", "F1", FEATURE)
    operate(
        "decompose",
        "F1",
        [
            linked("a", ["contribute"]),
            linked("v1", ["verify"], ["a"]),
            linked("v2", ["verify"], ["a"]),
        ],
        "F1",
    )
    approve_all()
    approve_direction()


def test_every_required_verifier_must_be_accounted_for(multiverify):
    work("a")
    assert status()["verification"] == "unresolved"
    work("v1")
    assert status()["implementation"] == "satisfied"
    assert status()["verification"] == "unresolved"  # v2 has not verified yet
    assert status()["status"] == "unresolved"
    assert [o["status"] for o in status()["obligations"] if o["role"] == "verify"] == [
        "satisfied",
        "unresolved",
    ]
    work("v2")
    work("F1")
    assert status()["verification"] == "satisfied" and status()["status"] == "satisfied"
    requirement_evidence.require_satisfied(feature())


def test_partial_verification_blocks_final_validation(multiverify):
    work("a")
    work("v1")
    operate("start", "F1", task_id="v2")
    record_attempt("F1", "v2")  # not yet completed
    with pytest.raises(ValueError, match="checkpoint|completed"):
        start_validation()
    assert status()["verification"] == "unresolved"


def test_reviewer_not_applicable_never_satisfies_coverage(multiverify):
    work("a")
    work("v1")
    work("v2")
    work("F1")
    # Keep only what a reviewer's not_applicable outcome leaves: the completed task, with the
    # association for it removed.
    dropped = copy.deepcopy(ledger.open_current("F1").state)
    dropped["evidence"] = [e for e in dropped["evidence"] if e["task_id"] != "v2"]
    ledger.open_current("F1").append("test-not-applicable", {}, dropped)
    assert status()["verification"] == "missing"
    assert next(o for o in status()["obligations"] if o["task_id"] == "v2")["status"] == "missing"
    path = ledger.open_current("F1").path
    before = path.read_bytes()
    for override in ({"basis": "not_applicable"}, {"result": "NOT_APPLICABLE"}):
        with pytest.raises(ValueError, match="result PASSED"):
            operate("evidence-record", "F1", payload("v2", "verify", **override), "v2")
    with pytest.raises(ValueError, match="evidence incomplete"):
        start_validation()
    assert path.read_bytes() == before
    # Only explicit, valid evidence for the completing attempt restores coverage.
    operate("evidence-record", "F1", payload("v2", "verify"), "v2")
    assert status()["status"] == "satisfied"
    start_validation()


# --- final validation and acceptance are gated on coverage ---------------------------------------


def _drop_verifier_evidence(state):
    state["evidence"] = [e for e in state["evidence"] if e["task_id"] != "v"]


def _stale_verifier_evidence(state):
    for entry in state["evidence"]:
        if entry["task_id"] == "v":
            entry["binding"] = "0" * 64


@pytest.mark.parametrize("damage", [_drop_verifier_evidence, _stale_verifier_evidence])
def test_validation_record_refuses_missing_or_stale_coverage(multi, damage):
    from test_feature_validation import result, start

    for task_id in ("a", "b", "v", "F1"):
        work(task_id)
    value = start()  # fully covered control: eligible so far
    broken = copy.deepcopy(ledger.open_current("F1").state)
    damage(broken)
    ledger.open_current("F1").append("test-damage", {}, broken)
    path = ledger.open_current("F1").path
    before = path.read_bytes()
    with pytest.raises(ValueError, match="evidence incomplete"):
        operate("validation-record", "F1", result(value))
    assert path.read_bytes() == before
    assert ledger.open_current("F1").state["validation"]["status"] == "validating"


@pytest.mark.parametrize("damage", [_drop_verifier_evidence, _stale_verifier_evidence])
def test_validation_accept_refuses_missing_or_stale_coverage(multi, damage):
    from test_feature_validation import result, start

    for task_id in ("a", "b", "v", "F1"):
        work(task_id)
    value = start()
    final = result(value)
    operate("validation-record", "F1", final)  # control: covered, so it records
    approval = operate("artifact-put", "F1", {"approved": "user"})["reference"]
    accept = {"attempt_id": "attempt", "binding": value, "approval": approval}
    broken = copy.deepcopy(ledger.open_current("F1").state)
    damage(broken)
    ledger.open_current("F1").append("test-damage", {}, broken)
    path = ledger.open_current("F1").path
    before = path.read_bytes()
    with pytest.raises(ValueError, match="evidence incomplete"):
        operate("validation-accept", "F1", accept)
    assert path.read_bytes() == before
    assert feature()["validation"]["status"] == "verified"  # never accepted


def test_fully_covered_feature_can_be_recorded_and_accepted(multi):
    from test_feature_validation import result, start

    for task_id in ("a", "b", "v", "F1"):
        work(task_id)
    value = start()
    operate("validation-record", "F1", result(value))
    approval = operate("artifact-put", "F1", {"approved": "user"})["reference"]
    operate(
        "validation-accept",
        "F1",
        {"attempt_id": "attempt", "binding": value, "approval": approval},
    )
    assert feature()["validation"]["status"] == "accepted"


# --- evidence-equivalence fails closed -------------------------------------------------------


@pytest.fixture
def rewritten(multi):
    from test_ledger_rules import rewrite_payload

    for task_id in ("a", "b", "v", "F1"):
        work(task_id)
    operate("history-rewrite", "F1", rewrite_payload(feature()))
    entry = next(e for e in feature()["evidence"] if e["task_id"] == "a")
    tree = feature()["tasks"]["a"]["checkpoint"]["tree"]
    proof = operate("artifact-put", "F1", {"equivalent": "proof"})["reference"]
    good = {
        "requirement": "REQ-1",
        "role": "contribute",
        "receipt": entry["receipt"],
        "tree": tree,
        "reference": proof,
    }
    return good, status()


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda g: {"reference": "sha256:" + "4" * 64}, "Artifact is missing"),
        (lambda g: {"receipt": "sha256:" + "5" * 64}, "No such recorded evidence"),
        (lambda g: {"requirement": "REQ-9"}, "No such recorded evidence"),
        (lambda g: {"role": "verify"}, "No such recorded evidence"),
        (lambda g: {"reference": "not-a-reference"}, "stored reference"),
        (lambda g: {"tree": "short"}, "full tree SHA"),
    ],
)
def test_equivalence_rejects_unstored_or_unknown_associations(rewritten, change, message):
    good, before_status = rewritten
    path = ledger.open_current("F1").path
    before = path.read_bytes()
    with pytest.raises(ValueError, match=message):
        operate("evidence-equivalence", "F1", {**good, **change(good)}, "a")
    assert path.read_bytes() == before
    assert status() == before_status


def test_equivalence_cannot_be_replaced_by_a_different_proof(rewritten):
    good, _ = rewritten
    operate("evidence-equivalence", "F1", good, "a")
    path = ledger.open_current("F1").path
    before = path.read_bytes()
    operate("evidence-equivalence", "F1", good, "a")  # identical replay
    assert path.read_bytes() == before
    other = operate("artifact-put", "F1", {"equivalent": "another proof"})["reference"]
    before = path.read_bytes()
    with pytest.raises(ValueError, match="already recorded differently"):
        operate("evidence-equivalence", "F1", {**good, "reference": other}, "a")
    assert path.read_bytes() == before


def test_equivalence_is_refused_when_the_tree_is_unchanged(multi):
    for task_id in ("a", "b", "v", "F1"):
        work(task_id)
    entry = next(e for e in feature()["evidence"] if e["task_id"] == "a")
    proof = operate("artifact-put", "F1", {"equivalent": "pointless"})["reference"]
    with pytest.raises(ValueError, match="already names this tree"):
        operate(
            "evidence-equivalence",
            "F1",
            {
                "requirement": "REQ-1",
                "role": "contribute",
                "receipt": entry["receipt"],
                "tree": entry["tree"],
                "reference": proof,
            },
            "a",
        )
