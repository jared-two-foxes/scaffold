"""Deterministic feature state. Evidence is supplied by callers, never executed here."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime

from ticket_pipeline.lib import feature_validation, issue_workflow, stack_store

VERSION = 2


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _strings(value: object, nonempty: bool = False) -> bool:
    return (
        isinstance(value, list) and (bool(value) or not nonempty) and all(_text(x) for x in value)
    )


def _fields(value: object, allowed: set[str]) -> None:
    if not isinstance(value, dict) or value.keys() - allowed:
        raise ValueError(f"Expected object with fields: {', '.join(sorted(allowed))}")


def _definition(value: dict, feature: bool = False) -> None:
    for field in ("id", "title", "objective"):
        if not _text(value.get(field)):
            raise ValueError(f"{field} must be a nonempty string")
    if not _strings(value.get("acceptance_criteria"), nonempty=True):
        raise ValueError("acceptance_criteria must be a nonempty string array")
    if not isinstance(value.get("context", ""), str):
        raise ValueError("context must be a string")
    if not _strings(value.get("constraints", [])):
        raise ValueError("constraints must be a string array")
    if feature and not _text(value.get("design_ref")):
        raise ValueError("design_ref must reference the approved design")


def _task(value: dict, feature_id: str, parent: str | None) -> dict:
    _fields(
        value,
        {
            "id",
            "title",
            "objective",
            "acceptance_criteria",
            "context",
            "constraints",
            "dependencies",
            "origin",
        },
    )
    _definition(value)
    if "origin" in value:
        origin = value["origin"]
        _fields(origin, {"kind", "reference"})
        if origin.get("kind") not in {"ticket", "design", "review", "discovery"} or not _text(
            origin.get("reference")
        ):
            raise ValueError("Issue origin requires kind and reference")
    dependencies = value.get("dependencies", [])
    if not _strings(dependencies) or len(set(dependencies)) != len(dependencies):
        raise ValueError("dependencies must be unique task identifiers")
    return {
        **value,
        "feature_id": feature_id,
        "parent_id": parent,
        "context": value.get("context", ""),
        "constraints": value.get("constraints", []),
        "dependencies": dependencies,
        "status": "pending",
        "history": [],
    }


def _prerequisites(task: dict, tasks: dict[str, dict]) -> set[str]:
    # Children inherit all ancestor dependencies, but never depend on the ancestor itself.
    required = set(task["dependencies"])
    parent = task["parent_id"]
    while parent is not None:
        required.update(tasks[parent]["dependencies"])
        parent = tasks[parent]["parent_id"]
    required.update(t["id"] for t in tasks.values() if t["parent_id"] == task["id"])
    return required


def _validate_graph(tasks: dict[str, dict]) -> None:
    for task in tasks.values():
        if any(dep not in tasks for dep in task["dependencies"]):
            raise ValueError(f"Unknown dependency for {task['id']}")
    visiting, visited = set(), set()

    def visit(identifier: str) -> None:
        if identifier in visiting:
            raise ValueError("Circular dependency (including parent/child ordering)")
        if identifier in visited:
            return
        visiting.add(identifier)
        for dep in _prerequisites(tasks[identifier], tasks):
            visit(dep)
        visiting.remove(identifier)
        visited.add(identifier)

    for identifier in tasks:
        visit(identifier)


def _ready(task: dict, tasks: dict[str, dict]) -> bool:
    parent = task["parent_id"]
    while parent is not None:
        if tasks[parent]["status"] in {"blocked", "failed"}:
            return False
        parent = tasks[parent]["parent_id"]
    return all(tasks[dep]["status"] == "completed" for dep in _prerequisites(task, tasks))


def _event(task: dict, status: str, **details: object) -> None:
    task["history"].append(
        {
            "from": task["status"],
            "to": status,
            "recorded_at": datetime.now(UTC).isoformat(),
            **details,
        }
    )
    task["status"] = status


def _evidence(value: object, feature_id: str, task_id: str) -> None:
    _fields(value, {"feature_id", "task_id", "result", "source", "reference", "summary"})
    if (
        value.get("feature_id") != feature_id
        or value.get("task_id") != task_id
        or value.get("result") != "PASSED"
        or not all(_text(value.get(key)) for key in ("source", "reference", "summary"))
    ):
        raise ValueError(
            "Completion requires task-bound PASSED evidence with source, reference, summary"
        )


def operate(
    action: str, feature_id: str, payload: object = None, task_id: str | None = None
) -> dict:
    """One locked read/validate/mutate/write transaction; reads never select or claim work."""
    if action not in feature_validation.ACTIONS | issue_workflow.ACTIONS | {
        "init",
        "add",
        "list",
        "next",
        "start",
        "complete",
        "block",
        "fail",
        "decompose",
        "retry",
    }:
        raise ValueError(f"Unknown action: {action}")
    path = stack_store.SCAFFOLD_DIR / "tasks.json"
    with stack_store._stack_lock():
        state = (
            json.loads(path.read_text(encoding="utf-8"))
            if path.exists()
            else {"schema_version": VERSION, "features": {}}
        )
        if (
            not isinstance(state, dict)
            or type(state.get("schema_version")) is not int
            or state["schema_version"] != VERSION
            or not isinstance(state.get("features"), dict)
        ):
            raise ValueError("Unsupported task state schema_version")
        features = state["features"]
        if action == "init":
            _fields(
                payload,
                {
                    "id",
                    "title",
                    "objective",
                    "design_ref",
                    "baseline",
                    "branch",
                    "acceptance_criteria",
                    "context",
                    "constraints",
                },
            )
            _definition(payload, feature=True)
            if payload["id"] != feature_id or feature_id in features:
                raise ValueError("Feature id mismatch or feature already exists")
            features[feature_id] = {
                **payload,
                "context": payload.get("context", ""),
                "constraints": payload.get("constraints", []),
                "tasks": {},
            }
        elif feature_id not in features:
            raise ValueError(f"Unknown feature: {feature_id}")
        feature = features[feature_id]
        tasks = feature["tasks"]
        if action == "init":
            feature["workflow"] = issue_workflow.KIND
            issue_workflow.initialize(feature, _task)
        if (
            not isinstance(feature.get("workflow"), dict)
            or feature["workflow"].get("kind") != issue_workflow.KIND
        ):
            raise ValueError("Unsupported feature state: recursive issue graph required")
        if action in issue_workflow.ACTIONS:
            previous_feature = copy.deepcopy(feature)
            selected = issue_workflow.transition(feature, action, payload, task_id, _event)
            if action != "design-next" and feature != previous_feature:
                feature["task_revision"] = feature.get("task_revision", 0) + 1
                if "validation" in feature:
                    feature["validation"].update(current=None)
                stack_store.atomic_write_json(path, state)
            result = {
                "schema_version": state["schema_version"],
                "feature": feature_validation.view(feature),
            }
            if action == "design-next":
                result["task"] = selected
            return result
        issue_workflow.guard(feature, action, task_id)
        if action in feature_validation.ACTIONS:
            feature_validation.transition(feature, action, payload)
            stack_store.atomic_write_json(path, state)
            return {
                "schema_version": state["schema_version"],
                "feature": feature_validation.view(feature),
            }
        if action in {"add", "decompose"}:
            if not isinstance(payload, list) or not payload:
                raise ValueError("Input must be a nonempty array of tasks")
            parent = None
            if action == "decompose":
                if task_id not in tasks:
                    raise ValueError(f"Unknown task: {task_id}")
                parent = tasks[task_id]
                if parent["status"] not in {
                    "pending",
                    "in_progress",
                    "failed",
                    "blocked",
                    "decomposed",
                }:
                    raise ValueError("Cannot decompose a completed task")
            for value in payload:
                task = _task(value, feature_id, task_id if parent is not None else None)
                if task["id"] in tasks:
                    raise ValueError(f"Duplicate task id: {task['id']}")
                tasks[task["id"]] = task
            _validate_graph(tasks)
            if parent is not None:
                issue_workflow.invalidate(parent, tasks)
                _event(parent, "decomposed", children=[t["id"] for t in payload])
        elif action in {"start", "complete", "block", "fail", "retry"}:
            if task_id not in tasks:
                raise ValueError(f"Unknown task: {task_id}")
            task = tasks[task_id]
            status = task["status"]
            if action == "complete":
                _evidence(payload, feature_id, task_id)
                if status == "completed":
                    if task["history"][-1]["evidence"] != payload:
                        raise ValueError("Task already completed with different evidence")
                    return {"schema_version": state["schema_version"], "task": copy.deepcopy(task)}
                if status != "in_progress" or not _ready(task, tasks):
                    raise ValueError("Completion requires in_progress and completed prerequisites")
                _event(task, "completed", evidence=payload)
            elif action == "start":
                if any(t["status"] == "in_progress" for t in tasks.values()):
                    raise ValueError("Another issue is already active")
                if any(
                    t["status"] == "completed" and not t.get("checkpoint") for t in tasks.values()
                ):
                    raise ValueError("Record verified checkpoints before starting another issue")
                if status not in {"pending", "decomposed"} or not _ready(task, tasks):
                    raise ValueError("Task is not ready to start")
                _event(task, "in_progress")
            elif action == "retry":
                if status not in {"in_progress", "blocked", "failed"}:
                    raise ValueError("Only interrupted, blocked or failed work can be retried")
                if not _text(payload):
                    raise ValueError("A reason is required")
                target = (
                    "decomposed"
                    if any(t["parent_id"] == task_id for t in tasks.values())
                    else "pending"
                )
                _event(task, target, reason=payload)
            else:
                allowed = (
                    {"pending", "in_progress", "decomposed"}
                    if action == "block"
                    else {"in_progress"}
                )
                if status not in allowed or not _text(payload):
                    raise ValueError("Invalid transition or missing reason")
                _event(task, "blocked" if action == "block" else "failed", reason=payload)
        if action in {"init", "add", "decompose", "start", "complete", "block", "fail", "retry"}:
            feature["task_revision"] = feature.get("task_revision", 0) + 1
            if "validation" in feature:
                feature["validation"].update(current=None)
            stack_store.atomic_write_json(path, state)
        if action in {"start", "complete", "block", "fail", "retry"}:
            return {
                "schema_version": state["schema_version"],
                "task": copy.deepcopy(tasks[task_id]),
            }
        if action == "next":
            workflow = feature["workflow"]
            if workflow["phase"] != "implement" or any(
                t["status"] == "in_progress"
                or (t["status"] == "completed" and not t.get("checkpoint"))
                for t in tasks.values()
            ):
                return {
                    "schema_version": state["schema_version"],
                    "feature": feature_validation.view(feature),
                    "task": None,
                }
            task = next(
                (
                    t
                    for t in sorted(tasks.values(), key=lambda t: -issue_workflow.depth(t, tasks))
                    if t["status"] in {"pending", "decomposed"}
                    and _ready(t, tasks)
                    and issue_workflow.design(t)
                    and (not t.get("suspension") or t["suspension"].get("handoff"))
                ),
                None,
            )
            return {
                "schema_version": state["schema_version"],
                "feature": feature_validation.view(feature),
                "task": copy.deepcopy(task),
            }
        return {
            "schema_version": state["schema_version"],
            "feature": feature_validation.view(feature),
        }
