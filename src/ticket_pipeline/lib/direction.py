"""Feature direction approval, recorded in the ledger.

The user's one approval of a feature's direction is a ledger entry bound to a digest of the
feature definition. Scaffold computes that digest itself, so a caller cannot approve a stale
or different definition. Implementation (``start``) is refused until the approval matches.
This records *that* approval was supplied; the native Pi tool is what obtains it from the user.
"""

from __future__ import annotations

import copy
import hashlib

from ticket_pipeline.lib import ledger
from ticket_pipeline.lib.refs import require

ACTIONS = {"direction-approve", "design-revise"}
DEFINITION = (
    "id",
    "title",
    "objective",
    "acceptance_criteria",
    "constraints",
    "context",
    "design_ref",
    "baseline",
    "branch",
)


REQUIREMENT_FIELDS = ("title", "objective", "acceptance_criteria", "constraints")


def _is_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def definition_digest(feature: dict) -> str:
    """Digest of the immutable feature definition and its identity (including the instance)."""
    scope = {key: feature.get(key) for key in DEFINITION}
    scope["instance_id"] = (feature.get("workflow") or {}).get("instance_id")
    return ledger.digest(scope)


def approve(feature: dict, payload: object) -> None:
    require(
        isinstance(payload, dict)
        and set(payload) <= {"scope_digest", "capabilities"}
        and "scope_digest" in payload,
        "Direction approval requires scope_digest (and optional capabilities)",
    )
    capabilities = payload.get("capabilities", [])
    require(
        isinstance(capabilities, list) and all(isinstance(c, str) and c for c in capabilities),
        "capabilities must be a list of nonempty strings",
    )
    require(
        payload["scope_digest"] == definition_digest(feature),
        "Approval does not match the current feature definition",
    )
    approved = {"scope_digest": payload["scope_digest"], "capabilities": capabilities}
    if feature.get("direction") == approved:
        return  # idempotent replay
    require("direction" not in feature, "Direction is already approved with different capabilities")
    feature["direction"] = approved


def revise(feature: dict, payload: object) -> None:
    """Replace the feature's design before the direction is approved.

    The user's alterations are applied here, so the design they finally approve is the exact
    text stored. ``context`` and ``design_ref`` are required; ``title``, ``objective``,
    ``acceptance_criteria`` and ``constraints`` may accompany them, and are replaced together
    with the context so a criterion removed from the design cannot stay mandatory. Refused
    once approved, and once any issue design work exists, because that work was derived from
    the previous text. ``design_ref`` must be the SHA-256 of the new context so the approved
    reference always names the stored design.
    """
    require(
        isinstance(payload, dict)
        and {"context", "design_ref"} <= set(payload)
        and set(payload) <= {"context", "design_ref", *REQUIREMENT_FIELDS}
        and isinstance(payload["context"], str)
        and payload["context"].strip(),
        "Design revision requires a nonempty context and its design_ref "
        "(optionally title, objective, acceptance_criteria, constraints)",
    )
    for field in ("title", "objective"):
        require(
            field not in payload or _is_text(payload[field]),
            f"{field} must be a nonempty string",
        )
    require(
        "acceptance_criteria" not in payload
        or (
            isinstance(payload["acceptance_criteria"], list)
            and payload["acceptance_criteria"]
            and all(_is_text(c) for c in payload["acceptance_criteria"])
        ),
        "acceptance_criteria must be a nonempty string array",
    )
    require(
        "constraints" not in payload
        or (
            isinstance(payload["constraints"], list)
            and all(_is_text(c) for c in payload["constraints"])
        ),
        "constraints must be a string array",
    )
    require("direction" not in feature, "Direction is already approved; the design is immutable")
    tasks = feature["tasks"]
    root = tasks.get(feature["workflow"]["root_id"])
    require(
        len(tasks) == 1
        and root is not None
        and not root.get("designs")
        and not root.get("design_history"),
        "Design revision is only possible before any issue design work exists",
    )
    expected = "sha256:" + hashlib.sha256(payload["context"].encode("utf-8")).hexdigest()
    require(payload["design_ref"] == expected, "design_ref does not match the revised context")
    for target in (feature, root):
        target["context"] = payload["context"]
        for field in REQUIREMENT_FIELDS:
            if field in payload:
                target[field] = copy.deepcopy(payload[field])
    feature["design_ref"] = expected
    root["origin"] = {"kind": "ticket", "reference": expected}
    feature["workflow"]["history"].append(
        {"event": "design-revised", "design_ref": expected}
    )


def require_approved(feature: dict) -> None:
    approval = feature.get("direction")
    require(
        isinstance(approval, dict) and approval.get("scope_digest") == definition_digest(feature),
        "Approve the feature direction before implementation",
    )
