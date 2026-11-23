"""Read-only issue projections; full state remains authoritative in Scaffold."""

import copy
import hashlib
import json
from collections import Counter

from ticket_pipeline.lib import coverage, direction, feature_validation

ACTIONS = {"summary", "issue", "select", "design-select", "requirements"}
REQUIREMENTS_SCHEMA = "scaffold.requirements/v1"


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
        "coverage": _coverage(feature),
        "requirements": _requirement_counts(feature),
        "validation_status": feature_validation.view(feature)["validation"]["status"],
    }


def _mapped(entry):
    return bool(entry["contributors"]) and bool(entry["verifiers"])


def _plan_status(plan, recorded):
    if not isinstance(recorded, dict):
        return "unrecorded"
    return "current" if recorded.get("digest") == plan["digest"] else "stale"


def _coverage(feature):
    plan = coverage.compute(feature)
    recorded = feature["workflow"].get("plan")
    return {
        "complete": not plan["problems"],
        "problems": plan["problems"],
        "plan_status": _plan_status(plan, recorded),
        "plan_digest": plan["digest"],
        "recorded_plan_digest": recorded["digest"] if isinstance(recorded, dict) else None,
    }


def _requirement_counts(feature):
    """Compact counts only; ``requirements`` inspects individual requirements on demand."""
    plan = coverage.compute(feature)["requirements"]
    unmapped = [rid for rid in sorted(plan) if not _mapped(plan[rid])]
    return {
        "total": len(plan),
        "mapped": len(plan) - len(unmapped),
        "unmapped": len(unmapped),
        "unmapped_ids": unmapped,
    }


def _evidence(task):
    """Reference to the task's current completion evidence, or None before completion."""
    if task["status"] != "completed":
        return None
    found = next((h["evidence"] for h in reversed(task["history"]) if "evidence" in h), None)
    if not found:
        return None
    return {k: found[k] for k in ("result", "source", "reference") if k in found}


def _holder(task):
    return {"id": task["id"], "status": task["status"], "evidence": _evidence(task)}


def requirements_view(feature, payload=None):
    """Feature-level requirement/traceability projection (read-only, deterministic).

    ``payload`` may carry ``ids`` (only those requirements) and/or ``unmapped`` (only those
    lacking a contributor or verifier). The contract is versioned and additive: later tickets
    may add fields to each requirement (for example richer ``evidence``) without breaking readers.
    """
    if payload is not None and (
        not isinstance(payload, dict)
        or set(payload) - {"ids", "unmapped"}
        or not isinstance(payload.get("ids", []), list)
        or not all(isinstance(i, str) for i in payload.get("ids", []))
        or not isinstance(payload.get("unmapped", False), bool)
    ):
        raise ValueError("requirements accepts {ids: [string], unmapped: boolean}")
    payload = payload or {}
    tasks = feature["tasks"]
    plan = coverage.compute(feature)
    records = {r["id"]: r for r in feature["requirements"]}
    wanted = payload.get("ids")
    if wanted is not None:
        unknown = sorted(set(wanted) - records.keys())
        if unknown:
            raise ValueError(f"Unknown requirement ids: {', '.join(unknown)}")
    selected = sorted(records if wanted is None else set(wanted))
    if payload.get("unmapped"):
        selected = [rid for rid in selected if not _mapped(plan["requirements"][rid])]
    items = []
    for rid in selected:
        entry = plan["requirements"][rid]
        missing = [
            role
            for role, key in (("contribute", "contributors"), ("verify", "verifiers"))
            if not entry[key]
        ]
        record = {k: copy.deepcopy(records[rid][k]) for k in ("id", "text", "source") if k in records[rid]}
        record.update(
            mapped=not missing,
            missing=missing,
            contributors=[_holder(tasks[i]) for i in entry["contributors"]],
            verifiers=[_holder(tasks[i]) for i in entry["verifiers"]],
            problems=[p for p in plan["problems"] if p.startswith(rid + ":")],
        )
        items.append(record)
    return {
        "schema": REQUIREMENTS_SCHEMA,
        "feature": {"id": feature["id"], "phase": feature["workflow"]["phase"]},
        "coverage": _coverage(feature),
        "counts": _requirement_counts(feature),
        "requirements": items,
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
        "requirements",
        "requirements_rationale",
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
    is_root = task["parent_id"] is None
    plan = coverage.compute(feature)
    records = {r["id"]: r for r in feature["requirements"]}
    own_links = coverage.links(task)
    # The root, while it still owns the decomposition, needs every requirement; any other issue
    # needs only those it is linked to (never sibling scopes).
    scope = sorted(records) if is_root else sorted(own_links)
    scoped = []
    for rid in scope:
        record = {k: copy.deepcopy(records[rid][k]) for k in ("id", "text", "source") if k in records[rid]}
        record.update(
            roles=sorted(own_links.get(rid, ())),
            contributors=plan["requirements"][rid]["contributors"],
            verifiers=plan["requirements"][rid]["verifiers"],
        )
        scoped.append(record)
    recorded = feature["workflow"].get("plan")
    binding = {
        "phase": feature["workflow"]["phase"],
        "task_revision": feature.get("task_revision", 0),
        "direction_scope": direction.definition_digest(feature),
        "plan_digest": plan["digest"],
        "plan_status": _plan_status(plan, recorded),
        "design_recorded": bool(task.get("designs")),
    }
    # Parent verification needs immediate children's contracts, never their histories.
    children = [contract(t) for t in tasks.values() if t["parent_id"] == identifier]
    return {
        "schema": "scaffold.issue-context/v1",
        "feature": summary(feature),
        "issue": own,
        "ancestors": ancestors,
        "prerequisites": [contract(tasks[d]) for d in sorted(dependencies)],
        "children": children,
        "requirements": scoped,
        "inherited_constraints": [
            {"from": a["id"], "constraints": a["constraints"]} for a in ancestors if a.get("constraints")
        ],
        "binding": binding,
        "details": {
            "operation": "list",
            "feature_id": feature["id"],
            "note": "Retrieve full history only when needed; issue retrieves one related contract",
        },
    }
