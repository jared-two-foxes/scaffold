"""Task attempts: the retained execution evidence a completion must point at.

Pi stores the full receipt as a ledger artifact and records the attempt here. Scaffold keeps
only the summary needed to enforce ordering, and the artifact reference (``receipt``) that a
later ``complete`` must cite. A completion therefore always names an attempt that exists in
this ledger, passed, and belongs to this task.
"""

from __future__ import annotations

from ticket_pipeline.lib.refs import is_commit, is_reference, require

ACTIONS = {"attempt-record"}
STATUSES = {"PASSED", "NEEDS_DESIGN", "BLOCKED"}
FIELDS = {"attempt", "status", "receipt", "base", "tree"}


def record(feature: dict, task_id: str | None, payload: object) -> None:
    require(task_id in feature["tasks"], "Unknown issue")
    task = feature["tasks"][task_id]
    require(
        isinstance(payload, dict) and set(payload) == FIELDS,
        "Attempt requires " + ", ".join(sorted(FIELDS)),
    )
    require(
        type(payload["attempt"]) is int
        and payload["attempt"] >= 1
        and payload["status"] in STATUSES
        and is_reference(payload["receipt"])
        and is_commit(payload["base"])
        and is_commit(payload["tree"]),
        "Invalid attempt record",
    )
    attempts = task.setdefault("attempts", [])
    if payload["attempt"] <= len(attempts):
        require(attempts[payload["attempt"] - 1] == payload, "Attempt already recorded differently")
        return  # idempotent replay
    require(task["status"] == "in_progress", "Attempts are recorded only for an active issue")
    require(payload["attempt"] == len(attempts) + 1, "Attempts must be recorded in sequence")
    require(
        all(a["receipt"] != payload["receipt"] for a in attempts),
        "Receipt already used by another attempt",
    )
    attempts.append(dict(payload))


def passed(task: dict, reference: object) -> bool:
    return any(
        a["status"] == "PASSED" and a["receipt"] == reference for a in task.get("attempts", [])
    )
