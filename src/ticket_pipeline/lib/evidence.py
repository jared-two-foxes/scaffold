"""Requirement evidence: structural associations between requirements and recorded attempts.

An association says "this task, in this role, produced a PASSED result for this requirement",
and names the retained attempt receipt, the attempt's tree, the design record and the coverage
plan it applies to. It is a *structural* claim. Scaffold checks that the association is
consistent with the ledger (known ids, ownership, the task's actual latest passing attempt, the
completing receipt, the checkpoint tree). It cannot authenticate the external execution behind
the receipt; Pi does that before recording.

Associations are append-only entries in ``feature["evidence"]``. They reuse the retained
artifacts, attempts and checkpoints already in the ledger (there is no second evidence store) and
are never deleted. Whether one still *applies* is derived from current state every time:

* ``satisfied``: the task is completed with exactly the receipt named, its checkpoint tree is the
  recorded tree (or an authenticated equivalence names that tree), the design record and the
  requirement's contributor/verifier binding are the ones it was recorded against, and the task still holds the role.
* ``pending``: recorded for the task's latest passing attempt, which has not completed yet.
* ``stale``: anything else (graph, design or history changed, a different attempt completed,
  the tree no longer matches). Stale records remain as history and satisfy nothing; fresh
  evidence or an explicit ``evidence-equivalence`` is required.

A requirement is satisfied only when every required contribution and verification is satisfied.
A task being complete, or a reviewer saying not applicable, never satisfies anything by itself.
"""

from __future__ import annotations

import copy
import hashlib
import json

from ticket_pipeline.lib import coverage
from ticket_pipeline.lib.refs import is_commit, is_reference, require

ACTIONS = {"evidence-record", "evidence-equivalence"}
RESULTS = {"PASSED"}
BASES = {"checks", "independent-review"}
FIELDS = {
    "feature_id",
    "instance_id",
    "requirement",
    "role",
    "result",
    "basis",
    "receipt",
    "tree",
    "binding",
    "design",
    "reference",
}
EQUIVALENCE_FIELDS = {"requirement", "role", "receipt", "tree", "reference"}
KEY = ("requirement", "task_id", "role", "receipt")

SATISFIED, PENDING, STALE = "satisfied", "pending", "stale"
MISSING, UNRESOLVED = "missing", "unresolved"
# Worst first: this is the status a requirement reports while anything is outstanding.
SEVERITY = (STALE, MISSING, UNRESOLVED)


def _design(task: dict) -> str | None:
    designs = task.get("designs")
    return designs[-1]["reference"] if designs else None


def _completion(task: dict) -> str | None:
    if task["status"] != "completed":
        return None
    found = next((h["evidence"] for h in reversed(task["history"]) if "evidence" in h), None)
    return found["reference"] if found else None


def _attempt(task: dict, receipt: object) -> dict | None:
    return next((a for a in task.get("attempts", []) if a["receipt"] == receipt), None)


def binding(plan: dict, requirement: str) -> str:
    """Digest of who must contribute to and verify one requirement.

    Unrelated graph changes leave it alone; any change to the requirement's contributors or
    verifiers (the design/graph facts the evidence depends on) makes recorded evidence stale.
    """
    entry = plan["requirements"].get(requirement, {})
    return hashlib.sha256(
        json.dumps(
            {"requirement": requirement, **entry}, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def _holds(plan: dict, requirement: str, task_id: str, role: str) -> bool:
    entry = plan["requirements"].get(requirement)
    key = "contributors" if role == coverage.CONTRIBUTE else "verifiers"
    return bool(entry) and task_id in entry[key]


def record(feature: dict, task_id: str | None, payload: object) -> None:
    require(task_id in feature["tasks"], "Unknown issue")
    task = feature["tasks"][task_id]
    require(
        isinstance(payload, dict) and set(payload) == FIELDS,
        "Evidence requires " + ", ".join(sorted(FIELDS)),
    )
    require(
        payload["feature_id"] == feature["id"]
        and payload["instance_id"] == feature["workflow"].get("instance_id"),
        "Evidence belongs to a different feature instance",
    )
    require(feature["workflow"]["phase"] == "implement", "Evidence is recorded during implementation")
    coverage.require_current(feature)
    plan = coverage.compute(feature)
    requirement, role = payload["requirement"], payload["role"]
    require(
        isinstance(requirement, str) and requirement in plan["requirements"],
        "Unknown requirement",
    )
    require(role in coverage.ROLES, "Unknown evidence role")
    require(
        _holds(plan, requirement, task_id, role),
        f"Issue {task_id} is not a required {role} for {requirement}",
    )
    require(
        payload["result"] in RESULTS and payload["basis"] in BASES,
        "Evidence needs result PASSED and basis checks or independent-review",
    )
    require(
        is_reference(payload["receipt"])
        and is_reference(payload["reference"])
        and is_commit(payload["tree"]),
        "Evidence needs stored receipt and reference artifacts and a full tree SHA",
    )
    require(
        payload["binding"] == binding(plan, requirement) and payload["design"] == _design(task),
        "Evidence is bound to a different design or requirement coverage binding",
    )
    require(task["status"] in {"in_progress", "completed"}, "Evidence needs started work")
    attempts = task.get("attempts", [])
    attempt = _attempt(task, payload["receipt"])
    require(
        attempt is not None and attempt["status"] == "PASSED",
        "Evidence must cite the receipt of a recorded PASSED attempt of this task",
    )
    require(attempt is attempts[-1], "Evidence cites a superseded attempt")
    if task["status"] == "completed":
        require(
            _completion(task) == payload["receipt"],
            "Evidence must cite the attempt that completed this issue",
        )
    require(attempt["tree"] == payload["tree"], "Evidence tree does not match the attempt's tree")
    entry = {"task_id": task_id, "attempt": attempts.index(attempt) + 1, **copy.deepcopy(payload)}
    existing = feature.setdefault("evidence", [])
    same = next((e for e in existing if all(e[k] == entry[k] for k in KEY)), None)
    if same is not None:
        require(
            all(same[k] == v for k, v in entry.items()),
            "Evidence already recorded differently for this requirement, task, role and receipt",
        )
        return  # idempotent replay
    existing.append(entry)


def equivalence(feature: dict, task_id: str | None, payload: object) -> None:
    """Carry recorded evidence onto a rewritten checkpoint tree, citing retained authentication."""
    require(task_id in feature["tasks"], "Unknown issue")
    task = feature["tasks"][task_id]
    require(
        isinstance(payload, dict) and set(payload) == EQUIVALENCE_FIELDS,
        "Equivalence requires " + ", ".join(sorted(EQUIVALENCE_FIELDS)),
    )
    require(
        is_reference(payload["reference"]) and is_commit(payload["tree"]),
        "Equivalence needs a stored reference and a full tree SHA",
    )
    checkpoint = task.get("checkpoint")
    require(
        checkpoint is not None and checkpoint["tree"] == payload["tree"],
        "Equivalence must name the issue's current checkpoint tree",
    )
    found = next(
        (
            e
            for e in feature.get("evidence", [])
            if e["task_id"] == task_id
            and e["requirement"] == payload["requirement"]
            and e["role"] == payload["role"]
            and e["receipt"] == payload["receipt"]
        ),
        None,
    )
    require(found is not None, "No such recorded evidence to carry over")
    if found["tree"] == payload["tree"]:
        raise ValueError("The evidence already names this tree")
    link = {"tree": payload["tree"], "reference": payload["reference"]}
    equivalents = found.setdefault("equivalent", [])
    if link in equivalents:
        return
    require(
        all(e["tree"] != payload["tree"] for e in equivalents),
        "Equivalence already recorded differently for this tree",
    )
    equivalents.append(link)


def _applicability(feature: dict, task: dict, entry: dict, plan: dict) -> str:
    if (
        entry["binding"] != binding(plan, entry["requirement"])
        or entry["design"] != _design(task)
        or not _holds(plan, entry["requirement"], task["id"], entry["role"])
    ):
        return STALE
    attempts = task.get("attempts", [])
    attempt = _attempt(task, entry["receipt"])
    if attempt is None or attempt["status"] != "PASSED" or attempt["tree"] != entry["tree"]:
        return STALE
    if task["status"] == "completed":
        if _completion(task) != entry["receipt"]:
            return STALE
        checkpoint = task.get("checkpoint")
        if checkpoint is None:
            return PENDING
        trees = {entry["tree"], *(e["tree"] for e in entry.get("equivalent", []))}
        return SATISFIED if checkpoint["tree"] in trees else STALE
    if task["status"] == "in_progress" and attempt is attempts[-1]:
        return PENDING
    return STALE


def _obligation(feature: dict, task: dict, requirement: str, role: str, plan: dict) -> dict:
    records = [
        (entry, _applicability(feature, task, entry, plan))
        for entry in feature.get("evidence", [])
        if entry["task_id"] == task["id"]
        and entry["requirement"] == requirement
        and entry["role"] == role
    ]
    states = {state for _, state in records}
    if SATISFIED in states:
        status = SATISFIED
    elif PENDING in states:
        status = UNRESOLVED
    elif records:
        status = STALE
    else:
        status = MISSING if task["status"] == "completed" else UNRESOLVED
    shown = next(
        (e for e, s in records if s == SATISFIED), next((e for e, s in records if s == PENDING), None)
    )
    return {
        "task_id": task["id"],
        "role": role,
        "status": status,
        "task_status": task["status"],
        "records": len(records),
        "evidence": (
            {k: shown[k] for k in ("receipt", "reference", "basis", "tree", "attempt")}
            if shown
            else None
        ),
    }


def _rollup(statuses: list[str]) -> str:
    if statuses and all(s == SATISFIED for s in statuses):
        return SATISFIED
    for candidate in SEVERITY:
        if candidate in statuses:
            return candidate
    return MISSING  # nothing required is assigned: no evidence can exist


def assess(feature: dict) -> dict[str, dict]:
    """Per-requirement implementation and verification coverage derived from the ledger."""
    tasks = feature["tasks"]
    plan = coverage.compute(feature)
    result = {}
    for record in feature["requirements"]:
        rid = record["id"]
        entry = plan["requirements"][rid]
        built = [
            _obligation(feature, tasks[i], rid, coverage.CONTRIBUTE, plan)
            for i in entry["contributors"]
        ]
        checked = [
            _obligation(feature, tasks[i], rid, coverage.VERIFY, plan) for i in entry["verifiers"]
        ]
        implementation = _rollup([o["status"] for o in built])
        verification = _rollup([o["status"] for o in checked])
        statuses = [o["status"] for o in built + checked]
        result[rid] = {
            "status": _rollup(statuses),
            "implementation": implementation,
            "verification": verification,
            # Checks alone never count as independent verification.
            "independent_verification": any(
                o["status"] == SATISFIED and o["evidence"]["basis"] == "independent-review"
                for o in checked
            ),
            "obligations": built + checked,
        }
    return result


def require_satisfied(feature: dict) -> None:
    """Final validation and acceptance need complete, applicable coverage of every requirement."""
    problems = list(coverage.compute(feature)["problems"]) + [
        f"{rid}: {item['status']} (implementation {item['implementation']}, "
        f"verification {item['verification']})"
        for rid, item in assess(feature).items()
        if item["status"] != SATISFIED
    ]
    require(not problems, "Requirement evidence incomplete: " + "; ".join(problems))


def require_for_completion(feature: dict, task_id: str, receipt: str) -> None:
    """A task is completed only with evidence for every requirement role it was assigned."""
    task = feature["tasks"][task_id]
    plan = coverage.compute(feature)
    missing = []
    for record in feature["requirements"]:
        for role, key in ((coverage.CONTRIBUTE, "contributors"), (coverage.VERIFY, "verifiers")):
            if task_id not in plan["requirements"][record["id"]][key]:
                continue
            ok = any(
                e["task_id"] == task_id
                and e["requirement"] == record["id"]
                and e["role"] == role
                and e["receipt"] == receipt
                and _applicability(feature, task, e, plan) == PENDING
                for e in feature.get("evidence", [])
            )
            if not ok:
                missing.append(f"{record['id']} ({role})")
    require(
        not missing,
        "Completion needs evidence for the completing attempt: " + ", ".join(missing),
    )
