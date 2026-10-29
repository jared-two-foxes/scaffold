import copy

import pytest

from test_tasks import FEATURE, evidence, finish, state, task  # noqa: F401
from ticket_pipeline.lib.task_store import operate


def binding():
    return {"feature_id": "F1", "design_ref": FEATURE["design_ref"], "baseline": "a" * 40,
            "head": "b" * 40, "tree": "c" * 40, "branch": "feature/test",
            "task_revision": operate("list", "F1")["feature"]["task_revision"],
            "plan_digest": "d" * 64}


def start(attempt="attempt"):
    value = binding()
    operate("validation-start", "F1", {"attempt_id": attempt, "binding": value})
    return value


def result(value, status="PASSED", attempt="attempt"):
    return {"schema": "pi.feature-validation/v1", "attempt_id": attempt, "binding": value,
            "status": status, "reference": "pi-feature-evidence:sha256:" + "e" * 64,
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
