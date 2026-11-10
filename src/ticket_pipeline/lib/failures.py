"""Failures, recorded in the ledger so a person can see how a run went wrong.

A pipeline tool that fails (blocked, needs a design change, or crashed) appends a
``failure-record`` entry. The entry is a log line, not a state machine step: it is accepted in any
phase for any open feature, and never blocks or unblocks work. The full record is the ledger
entry's body; the feature keeps only a short, bounded list of recent failures so a ``list`` shows
what has been going wrong without growing without limit.

``owner`` says who must act: ``agent`` (fix and retry), ``user`` (a decision or approval is
needed) or ``bug`` (the pipeline itself failed unexpectedly).
"""

from __future__ import annotations

import re

from ticket_pipeline.lib.refs import is_reference, require

ACTIONS = {"failure-record"}
OWNERS = {"agent", "user", "bug"}
FIELDS = {"code", "message", "tool", "owner", "stage", "cause", "evidence", "next"}
REQUIRED = {"code", "message", "tool", "owner"}
KEEP = 20  # recent failures kept in the feature state
CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
TOOL = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _text(value: object, limit: int) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit


def record(feature: dict, task_id: str | None, payload: object) -> None:
    require(
        isinstance(payload, dict) and REQUIRED <= set(payload) and set(payload) <= FIELDS,
        "A failure requires "
        + ", ".join(sorted(REQUIRED))
        + " (optional: stage, cause, evidence, next)",
    )
    require(
        isinstance(payload["code"], str) and CODE.match(payload["code"]),
        "code must be UPPER_SNAKE_CASE",
    )
    require(
        isinstance(payload["tool"], str) and TOOL.match(payload["tool"]),
        "tool must be a tool name",
    )
    require(payload["owner"] in OWNERS, "owner must be one of " + ", ".join(sorted(OWNERS)))
    require(
        _text(payload["message"], 2000), "message must be nonempty text of at most 2000 characters"
    )
    if "stage" in payload:
        require(_text(payload["stage"], 64), "stage must be nonempty text of at most 64 characters")
    if "cause" in payload:
        require(
            _text(payload["cause"], 4000), "cause must be nonempty text of at most 4000 characters"
        )
    if "evidence" in payload:
        require(is_reference(payload["evidence"]), "evidence must be a sha256 artifact reference")
    if "next" in payload:
        steps = payload["next"]
        require(
            isinstance(steps, list) and len(steps) <= 5 and all(_text(s, 300) for s in steps),
            "next must be at most 5 short steps",
        )
    if task_id is not None:
        require(task_id in feature["tasks"], "Unknown issue")
    entry = {
        "code": payload["code"],
        "tool": payload["tool"],
        "owner": payload["owner"],
        "message": payload["message"][:200],
    }
    if task_id is not None:
        entry["task_id"] = task_id
    if "stage" in payload:
        entry["stage"] = payload["stage"]
    recent = feature.setdefault("failures", [])
    recent.append(entry)
    del recent[:-KEEP]
