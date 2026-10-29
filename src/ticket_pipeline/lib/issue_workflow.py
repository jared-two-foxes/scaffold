"""Optional recursive issue design lifecycle. Callers supply approval and Git evidence."""

import copy
import hashlib
import json
import re
from datetime import UTC, datetime

KIND = "recursive-issues/v1"
ACTIONS = {
    "design-next",
    "design-record",
    "design-finish",
    "design-reopen",
    "checkpoint-record",
    "issue-resume",
}


def require(ok, message):
    if not ok:
        raise ValueError(message)


def event(workflow, name, **details):
    workflow["history"].append(
        {"event": name, "recorded_at": datetime.now(UTC).isoformat(), **details}
    )


def initialize(feature, make_task):
    require(feature["workflow"] == KIND, "Unsupported issue workflow")
    require(
        isinstance(feature.get("baseline"), str)
        and re.fullmatch(r"[a-f0-9]{40}", feature["baseline"]),
        "Full original baseline required",
    )
    require(
        isinstance(feature.get("branch"), str) and feature["branch"].strip(),
        "Feature branch required",
    )
    feature["original_baseline"] = feature["baseline"]
    root = make_task(
        {
            k: copy.deepcopy(feature[k])
            for k in ("id", "title", "objective", "acceptance_criteria", "context", "constraints")
        },
        feature["id"],
        None,
    )
    root["origin"] = {"kind": "ticket", "reference": feature["design_ref"]}
    feature["tasks"][root["id"]] = root
    feature["workflow"] = {"kind": KIND, "phase": "design", "root_id": root["id"], "history": []}
    event(feature["workflow"], "initialized")


def design(task):
    return task.get("designs", [])[-1] if task.get("designs") else None


def needs_design(task):
    return task["status"] != "completed" and design(task) is None


def depth(task, tasks):
    return 0 if task["parent_id"] is None else 1 + depth(tasks[task["parent_id"]], tasks)


def invalidate(task, tasks):
    affected = {task["id"]}
    while True:
        more = {
            t["id"]
            for t in tasks.values()
            if t["status"] != "completed"
            and (t["parent_id"] in affected or any(d in affected for d in t["dependencies"]))
        }
        if more <= affected:
            break
        affected.update(more)
    for identifier in affected:
        issue = tasks[identifier]
        if issue.get("designs"):
            issue.setdefault("design_history", []).extend(issue.pop("designs"))


def design_ready(task, tasks):
    if task["parent_id"] is not None and not design(tasks[task["parent_id"]]):
        return False
    return all(
        design(tasks[dep]) or tasks[dep]["status"] == "completed" for dep in task["dependencies"]
    )


def needs_suspension(task):
    """Lifecycle retry/failure never proves that an execution's source was parked."""
    return (
        task["status"] != "completed"
        and any(h.get("to") == "in_progress" for h in task["history"])
        and (not task.get("suspension") or task["suspension"].get("handoff"))
    )


def guard(feature, action, task_id=None):
    workflow = feature["workflow"]
    if action in {"add", "decompose"}:
        require(workflow["phase"] == "design", "Return to design before adding issues")
    if action in {"start", "complete", "decompose"}:
        require(task_id in feature["tasks"], "Unknown issue")
    if action == "decompose":
        require(
            not needs_suspension(feature["tasks"][task_id]),
            "Suspend active implementation with retained evidence before decomposition",
        )
    if action in {"start", "complete"}:
        require(
            workflow["phase"] == "implement", "Finish the entire design graph before implementation"
        )
        task = feature["tasks"][task_id]
        require(design(task), "Issue has no approved design")
        suspension = task.get("suspension")
        require(
            not suspension or suspension.get("handoff"),
            "Restore suspended issue through authenticated handoff",
        )


def transition(feature, action, payload, task_id, make_event):
    workflow = feature.get("workflow")
    require(
        isinstance(workflow, dict) and workflow["kind"] == KIND, "Feature is not an issue workflow"
    )
    tasks = feature["tasks"]
    if action == "design-next":
        require(workflow["phase"] == "design", "Not in design phase")
        for task in sorted(tasks.values(), key=lambda t: -depth(t, tasks)):
            if needs_design(task) and design_ready(task, tasks):
                return copy.deepcopy(task)
        return None
    if action == "design-finish":
        require(workflow["phase"] == "design", "Not in design phase")
        require(
            tasks and not any(needs_design(t) for t in tasks.values()),
            "Unapproved issue designs remain",
        )
        require(
            not any(t["status"] == "in_progress" for t in tasks.values()),
            "Active implementation remains",
        )
        workflow["phase"] = "implement"
        event(workflow, "design-finished")
        return
    if action == "design-reopen":
        require(
            isinstance(payload, dict)
            and set(payload) <= {"reason", "reference"}
            and isinstance(payload.get("reason"), str)
            and payload["reason"].strip(),
            "Reason required",
        )
        require(workflow["phase"] in {"design", "implement"}, "Invalid workflow phase")
        if workflow["phase"] == "design":
            require(
                task_id in tasks and design(tasks[task_id]),
                "During design, target an approved issue to reopen",
            )
        active = [t for t in tasks.values() if needs_suspension(t)]
        require(task_id is None or task_id in tasks, "Unknown issue")
        task = tasks.get(task_id) if task_id else None
        require(
            not active or (len(active) == 1 and task is active[0]), "Suspend the active issue first"
        )
        if task:
            require(
                task["status"] != "completed",
                "Completed issues retain history; add a dependent corrective issue",
            )
            if needs_suspension(task):
                require(
                    isinstance(payload.get("reference"), str)
                    and payload["reference"].startswith("pi-evidence:sha256:"),
                    "Active/failed issue requires retained native suspension evidence",
                )
                task["suspension"] = {"reference": payload["reference"]}
                make_event(
                    task,
                    "decomposed"
                    if any(t["parent_id"] == task_id for t in tasks.values())
                    else "pending",
                    reason=payload["reason"],
                    suspension=payload["reference"],
                )
            invalidate(task, tasks)
        workflow["phase"] = "design"
        event(workflow, "design-reopened", task_id=task_id, **payload)
        return
    require(task_id in tasks, "Unknown issue")
    task = tasks[task_id]
    if action == "design-record":
        require(
            workflow["phase"] == "design" and task["status"] in {"pending", "decomposed"},
            "Issue cannot be designed",
        )
        require(
            design_ready(task, tasks),
            "Design parent and prerequisite contracts first",
        )
        require(
            isinstance(payload, dict)
            and set(payload) == {"summary", "verification", "approval", "disposition"},
            "Design requires summary, verification, approval and disposition",
        )
        require(
            all(isinstance(payload[k], str) and payload[k].strip() for k in payload),
            "Nonempty design fields required",
        )
        children = [t["id"] for t in tasks.values() if t["parent_id"] == task_id]
        require(
            payload["disposition"] == ("decomposed" if children else "leaf"),
            "Disposition must match immediate children",
        )
        record = {**copy.deepcopy(payload), "children": children}
        record["reference"] = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    record, sort_keys=True, ensure_ascii=False, separators=(",", ":")
                ).encode()
            ).hexdigest()
        )
        if design(task):
            require(
                all(design(task).get(k) == v for k, v in record.items()),
                "Reopen design before changing an approved record",
            )
            return
        task["designs"] = [record]
        event(workflow, "design-approved", task_id=task_id, reference=record["reference"])
    elif action == "checkpoint-record":
        require(task["status"] == "completed", "Checkpoint requires completed issue")
        require(
            isinstance(payload, dict) and set(payload) == {"commit", "reference"},
            "Checkpoint requires commit and evidence reference",
        )

        require(
            isinstance(payload["commit"], str) and re.fullmatch(r"[a-f0-9]{40}", payload["commit"]),
            "Full commit SHA required",
        )
        require(
            payload["reference"] == task["history"][-1]["evidence"]["reference"],
            "Checkpoint evidence mismatch",
        )
        require(
            not task.get("checkpoint") or task["checkpoint"] == payload,
            "Checkpoint already recorded differently",
        )
        if task.get("checkpoint") == payload:
            return
        task["checkpoint"] = copy.deepcopy(payload)
    elif action == "issue-resume":
        require(
            workflow["phase"] == "implement" and design(task),
            "Approved implementation phase required",
        )
        require(
            task.get("suspension")
            and isinstance(payload, dict)
            and set(payload) == {"reference", "handoff"}
            and payload["reference"] == task["suspension"]["reference"]
            and isinstance(payload["handoff"], str)
            and payload["handoff"].startswith("pi-evidence:sha256:"),
            "Matching suspension/handoff required",
        )
        require(
            not task["suspension"].get("handoff")
            or task["suspension"]["handoff"] == payload["handoff"],
            "Issue already resumed differently",
        )
        if task["suspension"].get("handoff") == payload["handoff"]:
            return
        task["suspension"]["handoff"] = payload["handoff"]
        event(workflow, "issue-restored", task_id=task_id, **payload)
