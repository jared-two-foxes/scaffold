"""Feature validation associations; the native Pi gate authenticates execution evidence."""

import copy
import re
from datetime import UTC, datetime

ACTIONS = {"validation-start", "validation-record", "validation-invalidate"}
SHA = re.compile(r"[a-f0-9]{40}\Z")
REF = re.compile(r"pi-feature-evidence:sha256:[a-f0-9]{64}\Z")
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
    if validation["current"] is None:
        tasks = result["tasks"].values()
        validation["status"] = (
            "awaiting_validation" if tasks and all(t["status"] == "completed" for t in tasks)
            else "implementing"
        )
    return result


def transition(feature, action, payload):
    require(isinstance(payload, dict), "Validation payload must be an object")
    validation = feature.setdefault("validation", {"history": [], "current": None})
    if action == "validation-invalidate":
        require(set(payload) == {"reason"} and bool(payload["reason"]), "Invalidation requires reason")
        validation["history"].append({"event": "invalidated", **payload,
                                      "recorded_at": datetime.now(UTC).isoformat()})
        validation.update(current=None, status="awaiting_validation")
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
        require(feature.get("original_baseline", binding["baseline"]) == binding["baseline"],
                "Original feature baseline cannot change")
        eligible(feature)
        attempt = payload["attempt_id"]
        require(isinstance(attempt, str) and bool(attempt.strip()), "attempt_id required")
        existing = next((h for h in validation["history"] if h.get("attempt_id") == attempt), None)
        if existing:
            require(existing.get("binding") == binding, "Attempt identity conflict")
            return
        feature["original_baseline"] = binding["baseline"]
        validation["history"].append({"event": "started", **copy.deepcopy(payload),
                                      "recorded_at": datetime.now(UTC).isoformat()})
        validation.update(current=attempt, status="validating")
        return
    require(set(payload) == {"schema", "attempt_id", "binding", "status", "reference", "checks", "review", "message"},
            "Invalid feature result fields")
    require(payload["schema"] == "pi.feature-validation/v1", "Unsupported feature result schema")
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
        require(isinstance(review, dict) and review.get("schema") == "review.result/v1"
                and review.get("status") == "APPROVED" and review.get("reason") == "REVIEW_COMPLETED"
                and review.get("baseRef") == payload["binding"]["baseline"]
                and review.get("headRef") == payload["binding"]["head"]
                and bool(review.get("review_id")), "Matching independent feature review required")
    validation["history"].append({"event": "finished", "attempt_id": payload["attempt_id"],
                                  "result": copy.deepcopy(payload),
                                  "recorded_at": datetime.now(UTC).isoformat()})
    validation["status"] = {"PASSED": "verified", "NEEDS_DESIGN": "failed", "BLOCKED": "blocked"}[status]
    validation["reference"] = payload["reference"]
