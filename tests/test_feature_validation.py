import copy

import pytest

from test_tasks import FEATURE, evidence, finish, state, task  # noqa: F401
from ticket_pipeline.lib.task_store import operate


def binding():
    feature = operate("list", "F1")["feature"]
    return {"feature_id": "F1", "design_ref": FEATURE["design_ref"], "baseline": "a" * 40,
            "head": feature["head"], "tree": "c" * 40, "branch": "feature/test",
            "task_revision": operate("list", "F1")["feature"]["task_revision"],
            "plan_digest": "d" * 64}


def start(attempt="attempt"):
    value = binding()
    operate("validation-start", "F1", {"attempt_id": attempt, "binding": value})
    return value


def result(value, status="PASSED", attempt="attempt"):
    artifact = {"validation": attempt, "status": status}
    reference = operate("artifact-put", "F1", artifact)["reference"]
    return {"schema": "pi.feature-validation/v1", "attempt_id": attempt, "binding": value,
            "status": status, "reference": reference,
            "checks": [{"name": name, "exit_code": 0, "meaningful": True} for name in ("build", "test")],
            "review": {"schema": "review.result/v1", "status": "APPROVED", "reason": "REVIEW_COMPLETED",
                       "review_id": "independent", "baseRef": value["baseline"], "headRef": value["head"]},
            "message": "Complete approved criteria reviewed"}


def test_issue_state_eligibility_parents_and_completion(state):
    operate("add", "F1", [task("p")])
    operate("decompose", "F1", [task("child")], "p")
    with pytest.raises(ValueError, match="design|completed|checkpoint"):
        start()
    finish("child")
    with pytest.raises(ValueError, match="design|completed|checkpoint"):
        start()
    finish("p")
    feature = operate("list", "F1")["feature"]
    assert feature["validation"]["status"] == "awaiting_validation"
    value = start()
    assert operate("list", "F1")["feature"]["validation"]["status"] == "validating"
    final = result(value)
    operate("validation-record", "F1", final)
    before = state.read_bytes()
    operate("validation-record", "F1", final)
    assert state.read_bytes() == before
    validation = operate("list", "F1")["feature"]["validation"]
    assert validation["status"] == "verified"
    assert validation["reference"] == final["reference"]


def test_failure_correction_history_and_original_baseline(state):
    operate("add", "F1", [task("a")])
    finish("a")
    value = start()
    operate("validation-record", "F1", result(value, "NEEDS_DESIGN"))
    assert operate("list", "F1")["feature"]["validation"]["status"] == "failed"
    operate("design-reopen", "F1", {"reason": "Final review correction"})
    operate("add", "F1", [task("fix", ["a"])])
    assert operate("list", "F1")["feature"]["validation"]["status"] == "implementing"
    with pytest.raises(ValueError):
        start("second")
    finish("fix")
    fixed = binding()
    with pytest.raises(ValueError, match="baseline"):
        operate("validation-start", "F1", {"attempt_id": "wrong", "binding": {**fixed, "baseline": "f" * 40}})
    start("second")
    operate("validation-record", "F1", result(fixed, attempt="second"))
    history = operate("list", "F1")["feature"]["validation"]["history"]
    assert [h["result"]["status"] for h in history if h["event"] == "finished"] == ["NEEDS_DESIGN", "PASSED"]
    operate("validation-invalidate", "F1", {"reason": "HEAD/tree changed"})
    validation = operate("list", "F1")["feature"]["validation"]
    assert validation["status"] == "awaiting_validation"
    assert len(validation["history"]) == 5


@pytest.mark.parametrize("change", [
    lambda r: r.update(status="invented"),
    lambda r: r.update(reference="arbitrary.json"),
    lambda r: r.update(checks=[]),
    lambda r: r["checks"][0].update(exit_code=1),
    lambda r: r["checks"][1].update(meaningful=False),
    lambda r: r["review"].update(status="INDETERMINATE"),
    lambda r: r["review"].update(headRef="f" * 40),
    lambda r: r["binding"].update(task_revision=999),
    lambda r: r["binding"].update(design_ref="other"),
])
def test_reject_fabricated_or_mismatched_completions(state, change):
    operate("add", "F1", [task("a")])
    finish("a")
    value = start()
    payload = result(copy.deepcopy(value))
    change(payload)
    before = state.read_bytes()
    with pytest.raises(ValueError):
        operate("validation-record", "F1", payload)
    assert state.read_bytes() == before


def test_no_intent_no_success_and_interrupted_attempt_recovery(state):
    operate("add", "F1", [task("a")])
    finish("a")
    with pytest.raises(ValueError, match="intent"):
        operate("validation-record", "F1", result(binding()))
    value = start("interrupted")
    start("resumed")
    with pytest.raises(ValueError, match="intent"):
        operate("validation-record", "F1", result(value, attempt="interrupted"))
    operate("validation-record", "F1", result(value, "BLOCKED", "resumed"))
    start("fresh")
    operate("validation-record", "F1", result(value, attempt="fresh"))
    feature = operate("list", "F1")["feature"]
    assert feature["validation"]["status"] == "verified"
    assert len(feature["validation"]["history"]) == 5


def test_changed_graph_rejects_old_success_and_duplicate_tasks_do_not_invalidate(state):
    operate("add", "F1", [task("a")])
    finish("a")
    value = start()
    before = state.read_bytes()
    operate("complete", "F1", evidence("a"), "a")
    assert state.read_bytes() == before
    operate("design-reopen", "F1", {"reason": "Additional work"})
    operate("add", "F1", [task("b")])
    with pytest.raises(ValueError):
        operate("validation-record", "F1", result(value))


# --- Checks-only results: a full build/test pass with no whole-feature review is "checked", never "verified".

def checks_result(value, status="PASSED", attempt="attempt"):
    payload = result(value, status, attempt)
    payload.update(schema="pi.feature-checks/v1", review=None, message="Checks only; no feature review")
    return payload


def test_checks_only_pass_is_checked_not_verified_and_is_idempotent(state):
    operate("add", "F1", [task("a")])
    finish("a")
    assert operate("list", "F1")["feature"]["validation"]["status"] == "awaiting_validation"
    value = start()
    final = checks_result(value)
    operate("validation-record", "F1", final)
    validation = operate("list", "F1")["feature"]["validation"]
    assert validation["status"] == "checked"
    assert validation["reference"] == final["reference"]
    assert validation["history"][-1]["result"]["review"] is None
    before = state.read_bytes()
    operate("validation-record", "F1", final)
    assert state.read_bytes() == before


def test_checked_is_visible_in_summary_and_never_reported_as_verified(state):
    from ticket_pipeline.lib import scoped_queries
    operate("add", "F1", [task("a")])
    finish("a")
    operate("validation-record", "F1", checks_result(start()))
    feature = operate("list", "F1")["feature"]
    assert scoped_queries.summary(feature)["validation_status"] == "checked"


def test_checks_only_results_carry_no_review_and_reviewed_passes_still_need_one(state):
    operate("add", "F1", [task("a")])
    finish("a")
    value = start()
    # A checks-only result may not smuggle in a review, which would make "checked" look reviewed.
    with_review = checks_result(copy.deepcopy(value))
    with_review["review"] = result(copy.deepcopy(value))["review"]
    before = state.read_bytes()
    with pytest.raises(ValueError, match="no review"):
        operate("validation-record", "F1", with_review)
    # The reviewed kind cannot be recorded as PASSED without its review.
    no_review = result(copy.deepcopy(value))
    no_review["review"] = None
    with pytest.raises(ValueError, match="independent feature review"):
        operate("validation-record", "F1", no_review)
    with pytest.raises(ValueError, match="schema"):
        operate("validation-record", "F1", {**checks_result(copy.deepcopy(value)), "schema": "pi.feature-checks/v2"})
    assert state.read_bytes() == before
    assert operate("list", "F1")["feature"]["validation"]["status"] == "validating"


@pytest.mark.parametrize("change", [
    lambda r: r.update(status="invented"),
    lambda r: r.update(reference="arbitrary.json"),
    lambda r: r.update(checks=[]),
    lambda r: r["checks"][0].update(exit_code=1),
    lambda r: r["checks"][1].update(meaningful=False),
    lambda r: r["binding"].update(task_revision=999),
    lambda r: r["binding"].update(design_ref="other"),
])
def test_reject_fabricated_or_mismatched_checks_only_completions(state, change):
    operate("add", "F1", [task("a")])
    finish("a")
    value = start()
    payload = checks_result(copy.deepcopy(value))
    change(payload)
    before = state.read_bytes()
    with pytest.raises(ValueError):
        operate("validation-record", "F1", payload)
    assert state.read_bytes() == before


def test_checks_only_failure_and_block_use_the_shared_statuses(state):
    operate("add", "F1", [task("a")])
    finish("a")
    value = start("first")
    operate("validation-record", "F1", checks_result(value, "NEEDS_DESIGN", "first"))
    assert operate("list", "F1")["feature"]["validation"]["status"] == "failed"
    start("second")
    operate("validation-record", "F1", checks_result(value, "BLOCKED", "second"))
    assert operate("list", "F1")["feature"]["validation"]["status"] == "blocked"
    with pytest.raises(ValueError, match="no review"):
        start("third")
        operate("validation-record", "F1", {**checks_result(value, "BLOCKED", "third"),
                                             "review": result(value)["review"]})


def test_checked_is_invalidated_by_graph_changes_and_superseded_by_full_validation(state):
    operate("add", "F1", [task("a")])
    finish("a")
    value = start("checks")
    operate("validation-record", "F1", checks_result(value, attempt="checks"))
    assert operate("list", "F1")["feature"]["validation"]["status"] == "checked"
    # A later reviewed validation of the same head supersedes it.
    start("full")
    operate("validation-record", "F1", result(value, attempt="full"))
    assert operate("list", "F1")["feature"]["validation"]["status"] == "verified"
    # Any graph change makes the result inapplicable again (history is retained).
    operate("design-reopen", "F1", {"reason": "Additional work"})
    feature = operate("list", "F1")["feature"]
    assert feature["validation"]["current"] is None
    assert feature["validation"]["status"] == "awaiting_validation"  # every task is still completed
    assert [h["result"]["schema"] for h in feature["validation"]["history"] if h["event"] == "finished"] == [
        "pi.feature-checks/v1", "pi.feature-validation/v1"]  # both retained, in order
    operate("add", "F1", [task("b", ["a"])])
    assert operate("list", "F1")["feature"]["validation"]["status"] == "implementing"
    finish("b")
    value = start("again")
    operate("validation-record", "F1", checks_result(binding(), attempt="again"))
    assert operate("list", "F1")["feature"]["validation"]["status"] == "checked"
    operate("validation-invalidate", "F1", {"reason": "HEAD/tree changed"})
    assert operate("list", "F1")["feature"]["validation"]["status"] == "awaiting_validation"


# --- Acceptance: the user's sign-off on the current checked or verified result.

def approval(text="User accepted the finished feature"):
    return operate("artifact-put", "F1", {"approval": text})["reference"]


def acceptance(value, attempt="attempt", **extra):
    return {"attempt_id": attempt, "binding": value, "approval": approval(), **extra}


def checked_feature(attempt="attempt"):
    operate("add", "F1", [task("a")])
    finish("a")
    value = start(attempt)
    operate("validation-record", "F1", checks_result(value, attempt=attempt))
    return value


def verified_feature(attempt="attempt"):
    operate("add", "F1", [task("a")])
    finish("a")
    value = start(attempt)
    operate("validation-record", "F1", result(value, attempt=attempt))
    return value


def test_accepting_a_checked_result_is_recorded_idempotent_and_visible(state):
    from ticket_pipeline.lib import scoped_queries
    value = checked_feature()
    payload = acceptance(value, note="Reviewed the questions and merged")
    operate("validation-accept", "F1", payload)
    feature = operate("list", "F1")["feature"]
    validation = feature["validation"]
    assert validation["status"] == "accepted"
    assert validation["accepted"]["from_status"] == "checked"
    assert validation["accepted"]["approval"] == payload["approval"]
    assert validation["accepted"]["note"] == "Reviewed the questions and merged"
    assert validation["history"][-1]["event"] == "accepted"
    assert validation["history"][-1]["binding"] == value
    assert scoped_queries.summary(feature)["validation_status"] == "accepted"
    before = state.read_bytes()
    operate("validation-accept", "F1", payload)
    assert state.read_bytes() == before
    # A different approval or note for the same acceptance is refused, not silently replaced.
    with pytest.raises(ValueError, match="different approval"):
        operate("validation-accept", "F1", {**payload, "approval": approval("someone else")})
    with pytest.raises(ValueError, match="different approval"):
        operate("validation-accept", "F1", {**payload, "note": "another note"})
    assert state.read_bytes() == before


def test_a_verified_result_can_be_accepted_too(state):
    value = verified_feature()
    operate("validation-accept", "F1", acceptance(value))
    validation = operate("list", "F1")["feature"]["validation"]
    assert validation["status"] == "accepted"
    assert validation["accepted"]["from_status"] == "verified"


def test_only_a_current_passed_result_can_be_accepted(state):
    operate("add", "F1", [task("a")])
    finish("a")
    value = binding()
    # Nothing validated yet.
    with pytest.raises(ValueError, match="current checked or verified"):
        operate("validation-accept", "F1", acceptance(value))
    start("first")
    # Running, not finished.
    with pytest.raises(ValueError, match="current checked or verified"):
        operate("validation-accept", "F1", acceptance(value, "first"))
    operate("validation-record", "F1", checks_result(value, "NEEDS_DESIGN", "first"))
    with pytest.raises(ValueError, match="current checked or verified"):
        operate("validation-accept", "F1", acceptance(value, "first"))
    start("second")
    operate("validation-record", "F1", checks_result(value, "BLOCKED", "second"))
    with pytest.raises(ValueError, match="current checked or verified"):
        operate("validation-accept", "F1", acceptance(value, "second"))
    start("third")
    operate("validation-record", "F1", checks_result(value, attempt="third"))
    # An earlier attempt is not the current result, whatever its own outcome was.
    with pytest.raises(ValueError, match="current checked or verified"):
        operate("validation-accept", "F1", acceptance(value, "first"))
    assert operate("list", "F1")["feature"]["validation"]["status"] == "checked"


@pytest.mark.parametrize("change", [
    lambda p, v: p["binding"].update(head="e" * 40),
    lambda p, v: p["binding"].update(tree="e" * 40),
    lambda p, v: p["binding"].update(task_revision=v["task_revision"] + 1),
    lambda p, v: p["binding"].update(plan_digest="e" * 64),
    lambda p, v: p["binding"].pop("branch"),
    lambda p, v: p.update(approval="not-a-reference"),
    lambda p, v: p.update(approval="sha256:" + "0" * 64),   # well formed, but never stored
    lambda p, v: p.update(note="x" * 501),
    lambda p, v: p.update(note=7),
    lambda p, v: p.update(extra="field"),
    lambda p, v: p.pop("approval"),
    lambda p, v: p.update(attempt_id=""),
])
def test_acceptance_rejects_mismatched_unretained_or_malformed_requests(state, change):
    value = checked_feature()
    payload = acceptance(copy.deepcopy(value))
    change(payload, value)
    before = state.read_bytes()
    with pytest.raises(ValueError):
        operate("validation-accept", "F1", payload)
    assert state.read_bytes() == before
    assert operate("list", "F1")["feature"]["validation"]["status"] == "checked"


def test_acceptance_ends_with_the_result_it_signs_off(state):
    value = checked_feature()
    operate("validation-accept", "F1", acceptance(value))
    assert operate("list", "F1")["feature"]["validation"]["status"] == "accepted"
    # Any graph change makes it inapplicable; the history keeps the acceptance.
    operate("design-reopen", "F1", {"reason": "More work after acceptance"})
    validation = operate("list", "F1")["feature"]["validation"]
    assert validation["status"] == "awaiting_validation"
    assert "accepted" not in validation
    assert [h["event"] for h in validation["history"]][-1] == "accepted"
    with pytest.raises(ValueError, match="current checked or verified"):
        operate("validation-accept", "F1", acceptance(value))
    operate("add", "F1", [task("b", ["a"])])
    finish("b")
    fresh = start("again")
    operate("validation-record", "F1", checks_result(fresh, attempt="again"))
    operate("validation-accept", "F1", acceptance(fresh, "again"))
    assert operate("list", "F1")["feature"]["validation"]["status"] == "accepted"
    # An explicit invalidation also ends it, and removes the derived summary.
    operate("validation-invalidate", "F1", {"reason": "HEAD/tree changed"})
    validation = operate("list", "F1")["feature"]["validation"]
    assert validation["status"] == "awaiting_validation"
    assert "accepted" not in validation


def test_a_new_validation_attempt_supersedes_an_acceptance(state):
    value = checked_feature("first")
    operate("validation-accept", "F1", acceptance(value, "first"))
    operate("validation-start", "F1", {"attempt_id": "second", "binding": value})
    validation = operate("list", "F1")["feature"]["validation"]
    assert validation["status"] == "validating"
    assert "accepted" not in validation
    operate("validation-record", "F1", result(value, attempt="second"))
    # The earlier acceptance does not carry over to the new result: it needs its own sign-off.
    assert operate("list", "F1")["feature"]["validation"]["status"] == "verified"
    with pytest.raises(ValueError, match="current checked or verified"):
        operate("validation-accept", "F1", acceptance(value, "first"))
    operate("validation-accept", "F1", acceptance(value, "second"))
    assert operate("list", "F1")["feature"]["validation"]["status"] == "accepted"
