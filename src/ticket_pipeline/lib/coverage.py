"""Requirement links on recursive tasks and design-time coverage enforcement.

A task may carry ``requirements``: ``[{"id": "REQ-1", "roles": ["contribute", "verify"]}]``.
A task with no links is a *supporting* task and must say why in ``requirements_rationale``
(links and a rationale are mutually exclusive). The root has neither: its coverage is derived
from the graph below it.

Rules:

* Links name existing feature requirements, are unique per task, and use known roles.
* Roll-up: if a parent has links, every child link id must be one of the parent's ids (children
  narrow, never widen). A supporting parent has no requirement scope, so its children cannot
  claim requirements. Children of the root may claim any feature requirement.
* Coverage counts *executable* work only. A contributor is a leaf with the ``contribute`` role.
  A parent's ``contribute`` link is roll-up documentation and never substitutes for leaf
  coverage. A verifier is any task (leaf or parent) with the ``verify`` role, so explicitly
  designed parent integration verification counts. A root with no children is itself the one
  executable leaf and implicitly owns every requirement; once it has children it owns nothing.
* A verifier verifies the contributors it is the same as or dependency-ordered after (its
  transitive prerequisites, which include a parent's descendants). Every contributor needs such
  a verifier, and every verifier must be ordered after at least one contributor.

Results are deterministic: sorted ids, and a digest over the graph shape, links and the
resulting sets.
"""

from __future__ import annotations

import copy
import hashlib
import json

CONTRIBUTE = "contribute"
VERIFY = "verify"
ROLES = (CONTRIBUTE, VERIFY)
TASK_FIELDS = {"requirements", "requirements_rationale"}


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def normalize(value: dict) -> dict:
    """Validate the shape of a task's links and return the canonical task fields (sorted)."""
    out: dict = {}
    if "requirements" in value:
        links = value["requirements"]
        if not isinstance(links, list) or not links:
            raise ValueError("requirements must be a nonempty array of {id, roles}")
        seen: set[str] = set()
        canonical = []
        for index, link in enumerate(links):
            where = f"requirements[{index}]"
            if not isinstance(link, dict) or set(link) != {"id", "roles"}:
                raise ValueError(f"{where} must be an object with exactly id and roles")
            identifier, roles = link["id"], link["roles"]
            if not _text(identifier):
                raise ValueError(f"{where}.id must be a nonempty string")
            if (
                not isinstance(roles, list)
                or not roles
                or not all(isinstance(r, str) and r in ROLES for r in roles)
                or len(set(roles)) != len(roles)
            ):
                raise ValueError(
                    f"{where}.roles must be a nonempty unique list of: {', '.join(ROLES)}"
                )
            if identifier in seen:
                raise ValueError(f"Duplicate requirement link: {identifier}")
            seen.add(identifier)
            canonical.append({"id": identifier, "roles": sorted(roles)})
        out["requirements"] = sorted(canonical, key=lambda link: link["id"])
    if "requirements_rationale" in value:
        if not _text(value["requirements_rationale"]):
            raise ValueError("requirements_rationale must be a nonempty string")
        out["requirements_rationale"] = value["requirements_rationale"]
    return out


def links(task: dict) -> dict[str, set[str]]:
    return {link["id"]: set(link["roles"]) for link in task.get("requirements", [])}


def children(task_id: str, tasks: dict[str, dict]) -> list[str]:
    return sorted(t["id"] for t in tasks.values() if t["parent_id"] == task_id)


def prerequisites(task: dict, tasks: dict[str, dict]) -> set[str]:
    """Direct prerequisites: own and inherited ancestor dependencies, plus the task's children."""
    required = set(task["dependencies"])
    parent = task["parent_id"]
    while parent is not None:
        required.update(tasks[parent]["dependencies"])
        parent = tasks[parent]["parent_id"]
    required.update(children(task["id"], tasks))
    return required


def _before(tasks: dict[str, dict]) -> dict[str, set[str]]:
    """Transitive prerequisites of every task (the graph is acyclic when this is called)."""
    direct = {identifier: prerequisites(task, tasks) for identifier, task in tasks.items()}
    closure: dict[str, set[str]] = {}

    def visit(identifier: str) -> set[str]:
        if identifier not in closure:
            closure[identifier] = set()  # cycle guard; cycles are rejected before coverage
            reached: set[str] = set()
            for dep in direct[identifier]:
                reached.add(dep)
                reached |= visit(dep)
            closure[identifier] = reached
        return closure[identifier]

    for identifier in tasks:
        visit(identifier)
    return closure


def check_links(feature: dict) -> None:
    """Whole-graph consistency of links: unknown ids, presence, rationale and roll-up."""
    tasks = feature["tasks"]
    known = {record["id"] for record in feature["requirements"]}
    root_id = feature["workflow"]["root_id"]
    for identifier in sorted(tasks):
        task = tasks[identifier]
        own = links(task)
        unknown = sorted(own.keys() - known)
        if unknown:
            raise ValueError(
                f"Issue {identifier} links unknown requirement ids: {', '.join(unknown)}"
            )
        if identifier == root_id:
            if own or "requirements_rationale" in task:
                raise ValueError("The root issue's coverage is derived and takes no links")
            continue
        if own and "requirements_rationale" in task:
            raise ValueError(
                f"Issue {identifier} has requirement links and a rationale; "
                "a rationale is only for supporting issues with no links"
            )
        if not own and "requirements_rationale" not in task:
            raise ValueError(
                f"Issue {identifier} needs requirement links or a requirements_rationale "
                "explaining why it is a supporting issue"
            )
        parent = tasks.get(task["parent_id"]) if task["parent_id"] is not None else None
        if parent is None or parent["id"] == root_id:
            continue
        parent_links = links(parent)
        if not parent_links:
            if own:
                raise ValueError(
                    f"Issue {identifier} claims requirements but its parent {parent['id']} "
                    "is a supporting issue with no requirement scope"
                )
            continue
        outside = sorted(own.keys() - parent_links.keys())
        if outside:
            raise ValueError(
                f"Issue {identifier} links requirements outside its parent {parent['id']}: "
                f"{', '.join(outside)}"
            )


def compute(feature: dict) -> dict:
    """Deterministic per-requirement contributor/verifier sets, problems and a plan digest."""
    tasks = feature["tasks"]
    root_id = feature["workflow"]["root_id"]
    before = _before(tasks)
    leaves = sorted(i for i in tasks if not children(i, tasks))
    root_is_leaf = root_id in leaves
    per_task = {i: links(tasks[i]) for i in tasks}
    result: dict[str, dict] = {}
    problems: list[str] = []
    for record in feature["requirements"]:
        rid = record["id"]
        if root_is_leaf:
            contributors, verifiers = [root_id], [root_id]
        else:
            contributors = [i for i in leaves if CONTRIBUTE in per_task[i].get(rid, ())]
            verifiers = sorted(i for i in tasks if VERIFY in per_task[i].get(rid, ()))
        entry = {"contributors": contributors, "verifiers": verifiers}
        result[rid] = entry
        if not contributors:
            problems.append(f"{rid}: missing contributor (no executable issue implements it)")
        if not verifiers:
            problems.append(f"{rid}: missing verifier (no issue verifies it)")
        if not contributors or not verifiers:
            continue
        for contributor in contributors:
            if not any(v == contributor or contributor in before[v] for v in verifiers):
                problems.append(
                    f"{rid}: invalid verification order, contributor {contributor} is not "
                    f"verified by itself or a verifier ordered after it "
                    f"(verifiers: {', '.join(verifiers)})"
                )
        for verifier in verifiers:
            if not any(c == verifier or c in before[verifier] for c in contributors):
                problems.append(
                    f"{rid}: invalid verification order, verifier {verifier} is not the same "
                    f"as or ordered after any contributor "
                    f"(contributors: {', '.join(contributors)})"
                )
    shape = {
        i: {
            "parent": tasks[i]["parent_id"],
            "dependencies": sorted(tasks[i]["dependencies"]),
            "requirements": sorted(
                (rid, sorted(roles)) for rid, roles in per_task[i].items()
            ),
            "rationale": tasks[i].get("requirements_rationale"),
        }
        for i in sorted(tasks)
    }
    digest = hashlib.sha256(
        json.dumps(
            {"graph": shape, "coverage": result},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return {"requirements": result, "problems": problems, "digest": digest}


def require_complete(feature: dict) -> dict:
    """The coverage plan, or a refusal naming every missing requirement id and role."""
    plan = compute(feature)
    if plan["problems"]:
        raise ValueError("Design coverage incomplete: " + "; ".join(plan["problems"]))
    return plan


def require_current(feature: dict) -> None:
    """Refuse implementation from a plan that is missing or older than the graph."""
    recorded = feature["workflow"].get("plan")
    if not isinstance(recorded, dict):
        raise ValueError(
            "No design coverage plan is recorded; reopen the design and finish it again"
        )
    if recorded.get("digest") != compute(feature)["digest"]:
        raise ValueError("The recorded coverage plan is stale; the task graph changed since")


def snapshot(task: dict) -> dict:
    """The task's own links, recorded with its approved design."""
    return {k: copy.deepcopy(task[k]) for k in sorted(TASK_FIELDS) if k in task}
