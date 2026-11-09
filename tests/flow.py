"""Shared helpers: drive a task through approval, attempt, completion and checkpoint."""

from __future__ import annotations

import hashlib
import json

from ticket_pipeline.lib import ledger
from ticket_pipeline.lib.task_store import operate

_commits = iter(range(1, 10**6))


def sha(label: str) -> str:
    return ledger.digest(label)[:40]


def approve_direction(feature_id: str = "F1") -> dict:
    scope = operate("list", feature_id)["feature"]["direction_scope"]
    return operate(
        "direction-approve", feature_id, {"scope_digest": scope, "capabilities": ["test"]}
    )


def receipt_payload(task_id: str, number: int = 1) -> dict:
    return {"receipt": task_id} if number == 1 else {"receipt": task_id, "attempt": number}


def reference_for(task_id: str, number: int = 1) -> str:
    """The receipt reference of a task's Nth attempt (the first by default)."""
    blob = json.dumps(receipt_payload(task_id, number), ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()


def record_attempt(
    feature_id: str, task_id: str, status: str = "PASSED", number: int | None = None
):
    """Store a receipt artifact and record the attempt; returns the receipt reference."""
    feature = operate("list", feature_id)["feature"]
    attempts = feature["tasks"][task_id].get("attempts", [])
    number = number or len(attempts) + 1
    reference = operate("artifact-put", feature_id, receipt_payload(task_id, number))["reference"]
    assert reference == reference_for(task_id, number)
    operate(
        "attempt-record",
        feature_id,
        {
            "attempt": number,
            "status": status,
            "receipt": reference,
            "base": feature["head"],
            "tree": sha("tree-" + task_id),
        },
        task_id,
    )
    return reference


def record_checkpoint(feature_id: str, task_id: str, reference: str | None = None):
    feature = operate("list", feature_id)["feature"]
    commit = sha(f"commit-{task_id}-{next(_commits)}")
    return operate(
        "checkpoint-record",
        feature_id,
        {
            "commit": commit,
            "parent": feature["head"],
            "tree": sha("tree-" + task_id),
            "reference": reference or reference_for(task_id),
        },
        task_id,
    )
