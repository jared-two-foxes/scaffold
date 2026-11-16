"""Feature validation associations; the native Pi gate authenticates execution evidence."""

import copy
import re
from datetime import UTC, datetime

from ticket_pipeline.lib import direction, refs

ACTIONS = {"validation-start", "validation-record", "validation-invalidate", "validation-accept"}
SHA = re.compile(r"[a-f0-9]{40}\Z")
REF = re.compile(r"sha256:[a-f0-9]{64}\Z")
# Two kinds of final result. A reviewed validation carries an APPROVED independent review and a PASSED
# result is "verified". A checks-only result carries no review at all and a PASSED result is only
# "checked": the full build/test run passed with meaningful output, but nothing reviewed the feature
# as a whole. The distinct status keeps that difference visible instead of faking a review.
VALIDATION_SCHEMA = "pi.feature-validation/v1"
CHECKS_SCHEMA = "pi.feature-checks/v1"
PASSED_STATUS = {VALIDATION_SCHEMA: "verified", CHECKS_SCHEMA: "checked"}
BINDING = {"feature_id", "design_ref", "baseline", "head", "tree", "branch", "task_revision", "plan_digest"}


def require(ok, message):
    if not ok:
        raise ValueError(message)


def eligible(feature):
    require(feature["workflow"]["phase"] == "implement", "Finish issue graph design first")
    require(all(t.get("checkpoint") for t in feature["tasks"].values()), "Every issue needs a checkpoint")
    tasks = feature["tasks"]
    require(bool(tasks), "Feature has no required tasks")
    for task in tasks.values():
        require(task["status"] == "completed", "Every task, including parents, must be completed")
        evidence = task["history"][-1].get("evidence", {})
        require(
            evidence.get("feature_id") == feature["id"]
            and evidence.get("task_id") == task["id"]
            and evidence.get("source") == "conductor"
            and evidence.get("result") == "PASSED"
            and bool(evidence.get("reference")),
            "Missing task-bound successful Conductor evidence",
        )


def view(feature):
    result = copy.deepcopy(feature)
    validation = result.setdefault("validation", {"history": [], "current": None})
    validation.setdefault("history", [])
    validation.setdefault("current", None)
    result.setdefault("task_revision", 0)
    result["direction_scope"] = direction.definition_digest(feature)
    if validation["current"] is None:
        tasks = result["tasks"].values()
        validation["status"] = (
            "awaiting_validation" if tasks and all(t["status"] == "completed" for t in tasks)
            else "implementing"
        )
    elif validation.get("status") == "accepted":
        # Derived from the history event, so it can never outlive the acceptance it describes.
        event = next((h for h in reversed(validation["history"])
                      if h.get("event") == "accepted" and h.get("attempt_id") == validation["current"]), None)
        if event:
            validation["accepted"] = {k: event[k] for k in ("attempt_id", "approval", "from_status", "note",
                                                              "recorded_at") if k in event}
    return result


ACCEPTABLE = {"checked", "verified"}
NOTE_LIMIT = 500


def _accept(feature, validation, payload):
    """Record the user's sign-off on the current checked or verified result.

    Scaffold cannot authenticate a person: Pi confirms the user's approval and retains it as a stored
    artifact, and this action only records that reference against exactly what was validated. The
    acceptance applies to that result's binding (head, tree and graph revision) and ends with it: any
    later graph or history change, invalidation, or new validation attempt supersedes it.
    """
    required = {"attempt_id", "binding", "approval"}
    require(isinstance(payload, dict) and required <= set(payload) <= required | {"note"},
            "Acceptance requires attempt_id, binding and approval, and may carry a note")
    attempt = payload["attempt_id"]
    require(isinstance(attempt, str) and bool(attempt.strip()), "attempt_id required")
    require(refs.is_reference(payload["approval"]), "A retained user approval is required")
    note = payload.get("note")
    require(note is None or (isinstance(note, str) and len(note) <= NOTE_LIMIT),
            f"note must be text of at most {NOTE_LIMIT} characters")
    accepted = next((h for h in validation["history"]
                     if h.get("event") == "accepted" and h.get("attempt_id") == attempt), None)
    if accepted and validation.get("current") == attempt and validation.get("status") == "accepted":
        # A replay must restate the whole recorded acceptance, binding included; anything else is not a replay.
        require(accepted["approval"] == payload["approval"] and accepted.get("note") == note
                and accepted["binding"] == payload["binding"],
                "Already accepted with a different approval, note or binding")
        return  # idempotent replay
    require(validation.get("current") == attempt and validation.get("status") in ACCEPTABLE,
            "Only the current checked or verified result can be accepted")
    finished = next((h for h in validation["history"]
                     if h.get("event") == "finished" and h.get("attempt_id") == attempt), None)
    require(finished is not None and finished["result"]["status"] == "PASSED",
            "Only a passed result can be accepted")
    require(payload["binding"] == finished["result"]["binding"],
            "Acceptance must name exactly the binding that was validated")
    binding = finished["result"]["binding"]
    require(binding["task_revision"] == feature.get("task_revision", 0), "Task graph changed")
    require(binding["head"] == feature["head"] and feature["base"] == binding["baseline"],
            "The validated head is no longer the ledger's recorded head")
    eligible(feature)
    event = {"event": "accepted", "attempt_id": attempt, "binding": copy.deepcopy(binding),
             "approval": payload["approval"], "from_status": validation["status"],
             "recorded_at": datetime.now(UTC).isoformat()}
    if note is not None:
        event["note"] = note
    validation["history"].append(event)
    validation["status"] = "accepted"


def transition(feature, action, payload):
    require(isinstance(payload, dict), "Validation payload must be an object")
    validation = feature.setdefault("validation", {"history": [], "current": None})
    if action == "validation-invalidate":
        require(set(payload) == {"reason"} and bool(payload["reason"]), "Invalidation requires reason")
        validation["history"].append({"event": "invalidated", **payload,
                                      "recorded_at": datetime.now(UTC).isoformat()})
        validation.update(current=None, status="awaiting_validation")
        return
    if action == "validation-accept":
        _accept(feature, validation, payload)
        return
    if action == "validation-start":
        require(set(payload) == {"attempt_id", "binding"}, "Expected attempt_id and binding")
        binding = payload["binding"]
        require(isinstance(binding, dict) and set(binding) == BINDING, "Invalid validation binding")
        require(all(isinstance(binding[k], str) and binding[k] for k in BINDING - {"task_revision"}),
                "Binding fields must be nonempty strings")
        require(all(SHA.fullmatch(binding[k]) for k in ("baseline", "head", "tree")), "Full Git SHAs required")
        require(re.fullmatch(r"[a-f0-9]{64}", binding["plan_digest"]), "Invalid plan digest")
        require(binding["feature_id"] == feature["id"] and binding["design_ref"] == feature["design_ref"],
                "Feature/design mismatch")
        require(type(binding["task_revision"]) is int
                and binding["task_revision"] == feature.get("task_revision", 0), "Task graph changed")
        require(feature["base"] == binding["baseline"],
                "Validation baseline must be the ledger's history base (the fork point, "
                "or the commit the history was last rebased onto)")
        eligible(feature)
        require(
            binding["head"] == feature["head"],
            "Validation head must match the ledger's recorded history head; "
            "record foreign commits or rewrites first",
        )
        attempt = payload["attempt_id"]
        require(isinstance(attempt, str) and bool(attempt.strip()), "attempt_id required")
        existing = next((h for h in validation["history"] if h.get("attempt_id") == attempt), None)
        if existing:
            require(existing.get("binding") == binding, "Attempt identity conflict")
            return
        validation["history"].append({"event": "started", **copy.deepcopy(payload),
                                      "recorded_at": datetime.now(UTC).isoformat()})
        validation.update(current=attempt, status="validating")
        return
    require(set(payload) == {"schema", "attempt_id", "binding", "status", "reference", "checks", "review", "message"},
            "Invalid feature result fields")
    schema = payload["schema"]
    require(isinstance(schema, str) and schema in PASSED_STATUS, "Unsupported feature result schema")
    status = payload["status"]
    require(status in {"PASSED", "NEEDS_DESIGN", "BLOCKED"}, "Invalid final validation status")
    require(isinstance(payload["reference"], str) and REF.fullmatch(payload["reference"]),
            "Durable feature evidence reference required")
    previous = next((h for h in validation["history"] if h.get("event") == "finished"
                     and h.get("attempt_id") == payload["attempt_id"]), None)
    if previous:
        require(previous["result"] == payload, "Attempt already recorded with different result")
        return
    started = next((h for h in validation["history"] if h.get("event") == "started"
                    and h.get("attempt_id") == payload["attempt_id"]), None)
    require(started and started["binding"] == payload["binding"]
            and validation["current"] == payload["attempt_id"], "No matching active validation intent")
    require(payload["binding"]["task_revision"] == feature.get("task_revision", 0), "Task graph changed")
    if status == "PASSED":
        eligible(feature)
        checks = payload["checks"]
        require(isinstance(checks, list) and len(checks) >= 2
                and {"build", "test"} <= {c.get("name") for c in checks if isinstance(c, dict)}
                and all(isinstance(c, dict) and type(c.get("exit_code")) is int
                        and c["exit_code"] == 0 and c.get("meaningful") is True for c in checks),
                "Successful meaningful full build/test results required")
        review = payload["review"]
        if schema == CHECKS_SCHEMA:
            require(review is None, "A checks-only result carries no review")
        else:
            require(isinstance(review, dict) and review.get("schema") == "review.result/v1"
                    and review.get("status") == "APPROVED" and review.get("reason") == "REVIEW_COMPLETED"
                    and review.get("baseRef") == payload["binding"]["baseline"]
                    and review.get("headRef") == payload["binding"]["head"]
                    and bool(review.get("review_id")), "Matching independent feature review required")
    elif schema == CHECKS_SCHEMA:
        require(payload["review"] is None, "A checks-only result carries no review")
    validation["history"].append({"event": "finished", "attempt_id": payload["attempt_id"],
                                  "result": copy.deepcopy(payload),
                                  "recorded_at": datetime.now(UTC).isoformat()})
    validation["status"] = {"PASSED": PASSED_STATUS[schema], "NEEDS_DESIGN": "failed", "BLOCKED": "blocked"}[status]
    validation["reference"] = payload["reference"]
