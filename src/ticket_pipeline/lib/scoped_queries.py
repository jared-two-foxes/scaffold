"""Read-only issue projections; full state remains authoritative in Scaffold."""

import copy
import hashlib
import json
from collections import Counter

from ticket_pipeline.lib import feature_validation

ACTIONS = {"summary", "issue", "select", "design-select"}


def summary(feature):
    stamp = hashlib.sha256(
        json.dumps(feature, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "id": feature["id"],
        "title": feature["title"],
        "design_ref": feature["design_ref"],
        "baseline": feature["original_baseline"],
        "branch": feature["branch"],
        "phase": feature["workflow"]["phase"],
        "root_id": feature["workflow"]["root_id"],
        "needs_design": sum(
            t["status"] != "completed" and not t.get("designs") for t in feature["tasks"].values()
        ),
        "awaiting_checkpoint": sum(
            t["status"] == "completed" and not t.get("checkpoint")
            for t in feature["tasks"].values()
        ),
        "task_revision": feature.get("task_revision", 0),
        "state_digest": stamp,
        "counts": dict(Counter(t["status"] for t in feature["tasks"].values())),
        "validation_status": feature_validation.view(feature)["validation"]["status"],
    }


def contract(task):
    keys = (
        "id",
        "title",
        "objective",
        "acceptance_criteria",
        "constraints",
        "status",
        "parent_id",
        "dependencies",
        "origin",
        "checkpoint",
        "suspension",
    )
    result = {k: copy.deepcopy(task[k]) for k in keys if k in task}
    if task.get("designs"):
        result["design"] = copy.deepcopy(task["designs"][-1])
    completion = next((h["evidence"] for h in reversed(task["history"]) if "evidence" in h), None)
    if completion:
        result["completion"] = {k: completion[k] for k in ("result", "source", "reference")}
    return result


def issue(feature, identifier):
    tasks = feature["tasks"]
    if identifier not in tasks:
        raise ValueError("Unknown issue")
    task = tasks[identifier]
    ancestors, dependencies = [], set(task["dependencies"])
    parent = task["parent_id"]
    while parent is not None:
        ancestor = tasks[parent]
        ancestors.append(contract(ancestor))
        dependencies.update(ancestor["dependencies"])
        parent = ancestor["parent_id"]
    own = contract(task)
    own["context"] = task["context"]
    # Parent verification needs immediate children's contracts, never their histories.
    children = [contract(t) for t in tasks.values() if t["parent_id"] == identifier]
    return {
        "schema": "scaffold.issue-context/v1",
        "feature": summary(feature),
        "issue": own,
        "ancestors": ancestors,
        "prerequisites": [contract(tasks[d]) for d in sorted(dependencies)],
        "children": children,
        "details": {
            "operation": "list",
            "feature_id": feature["id"],
            "note": "Retrieve full history only when needed; issue retrieves one related contract",
        },
    }
