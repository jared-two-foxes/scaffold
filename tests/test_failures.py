"""``failure-record``: failures are ledger entries a person can read, accepted in any phase."""

import json

import pytest
from test_cli_ledger import feature_args, run
from test_ledger_rules import fresh  # noqa: F401  (fixture)
from test_tasks import FEATURE, task

from ticket_pipeline.lib import ledger
from ticket_pipeline.lib.task_store import operate

GOOD = {
    "code": "CHECK_FAILED",
    "message": "The test check failed: 2 of 12 tests failed",
    "tool": "execute_and_review",
    "owner": "agent",
    "stage": "VERIFICATION",
    "cause": "AssertionError: expected 2 got 3 at test/foo.test.js:14",
    "next": ["Fix the failing test", "Continue with previousAttempt"],
}


def listed():
    return operate("list", "F1")["feature"]


def entries(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_a_failure_is_recorded_in_any_phase_without_changing_work(fresh):  # noqa: F811
    before = listed()
    result = operate("failure-record", "F1", GOOD, "a")["feature"]
    assert result["failures"] == [
        {
            "code": "CHECK_FAILED",
            "tool": "execute_and_review",
            "owner": "agent",
            "message": GOOD["message"],
            "task_id": "a",
            "stage": "VERIFICATION",
        }
    ]
    after = listed()
    # Nothing but the failure list changed: no status, head, history or revision moved.
    for key in ("tasks", "head", "base", "history", "task_revision", "workflow"):
        assert after[key] == before[key]
    # The full record, including the cause, is the ledger entry's body.
    last = entries(ledger.open_current("F1").path)[-1]
    assert last["type"] == "failure-record" and last["body"] == {"payload": GOOD, "task": "a"}


def test_a_failure_is_recorded_before_direction_is_approved_and_for_the_feature(fresh):  # noqa: F811
    minimal = {
        "code": "NEEDS_APPROVAL",
        "message": "Approve the direction",
        "tool": "scaffold_tasks",
        "owner": "user",
    }
    operate("failure-record", "F1", minimal)
    assert listed()["failures"][0]["code"] == "NEEDS_APPROVAL"
    assert "task_id" not in listed()["failures"][0]


@pytest.mark.parametrize(
    "change",
    [
        {"owner": "nobody"},
        {"code": "lowercase"},
        {"code": "AB"},
        {"tool": "Not A Tool"},
        {"message": " "},
        {"message": "x" * 2001},
        {"cause": "x" * 4001},
        {"stage": ""},
        {"next": ["a"] * 6},
        {"next": "do it"},
        {"evidence": "not-a-reference"},
        {"surprise": "field"},
    ],
)
def test_a_malformed_failure_is_refused_and_leaves_the_ledger_unchanged(fresh, change):  # noqa: F811
    size = fresh.stat().st_size
    with pytest.raises(ValueError):
        operate("failure-record", "F1", {**GOOD, **change})
    assert fresh.stat().st_size == size


def test_required_fields_and_known_tasks_are_enforced(fresh):  # noqa: F811
    for missing in ("code", "message", "tool", "owner"):
        with pytest.raises(ValueError):
            operate("failure-record", "F1", {k: v for k, v in GOOD.items() if k != missing})
    with pytest.raises(ValueError, match="Unknown issue"):
        operate("failure-record", "F1", GOOD, "nope")
    with pytest.raises(ValueError):
        operate("failure-record", "F1", "just a string")


def test_evidence_must_be_a_stored_artifact(fresh):  # noqa: F811
    missing = "sha256:" + "0" * 64
    with pytest.raises(ValueError):
        operate("failure-record", "F1", {**GOOD, "evidence": missing})
    stored = operate("artifact-put", "F1", {"receipt": "r"})["reference"]
    operate("failure-record", "F1", {**GOOD, "evidence": stored})
    assert len(listed()["failures"]) == 1


def test_only_recent_failures_are_kept_in_state_but_every_one_stays_in_the_ledger(fresh):  # noqa: F811
    for number in range(25):
        operate("failure-record", "F1", {**GOOD, "message": f"failure {number}"})
    recent = listed()["failures"]  # re-read from the ledger: the fold gives the same bounded list
    assert [f["message"] for f in recent] == [f"failure {n}" for n in range(5, 25)]
    logged = [e for e in entries(fresh) if e["type"] == "failure-record"]
    assert len(logged) == 25 and logged[0]["body"]["payload"]["message"] == "failure 0"


def test_long_messages_are_shortened_in_state_only(fresh):  # noqa: F811
    long = "x" * 1500
    operate("failure-record", "F1", {**GOOD, "message": long})
    assert len(listed()["failures"][0]["message"]) == 200
    assert entries(fresh)[-1]["body"]["payload"]["message"] == long


def test_the_chain_still_verifies_after_failures(fresh):  # noqa: F811
    operate("failure-record", "F1", GOOD)
    operate("failure-record", "F1", GOOD, "a")
    assert listed()["failures"]  # opening re-verifies every hash and state digest


def test_through_the_command_line_the_task_is_optional(tmp_path):
    run(tmp_path, "init", *feature_args("--input", "-"), payload=FEATURE)
    run(tmp_path, "decompose", *feature_args("--task", "F1", "--input", "-"), payload=[task("a")])
    out = run(tmp_path, "failure-record", *feature_args("--input", "-"), payload=GOOD)
    assert out["feature"]["failures"][0]["code"] == "CHECK_FAILED"
    with_task = run(
        tmp_path, "failure-record", *feature_args("--task", "a", "--input", "-"), payload=GOOD
    )
    assert with_task["feature"]["failures"][-1]["task_id"] == "a"
    refused = run(
        tmp_path,
        "failure-record",
        *feature_args("--input", "-"),
        payload={**GOOD, "owner": "x"},
        ok=False,
    )
    assert "owner" in refused["error"]
