"""Feature direction approval, recorded in the ledger.

The user's one approval of a feature's direction is a ledger entry bound to a digest of the
feature definition. Scaffold computes that digest itself, so a caller cannot approve a stale
or different definition. Implementation (``start``) is refused until the approval matches.
This records *that* approval was supplied; the native Pi tool is what obtains it from the user.
"""

from __future__ import annotations

import copy
import hashlib

from ticket_pipeline.lib import ledger, requirements
from ticket_pipeline.lib.refs import require

ACTIONS = {"direction-approve", "design-revise"}
DEFINITION = (
    "id",
    "title",
    "objective",
    "requirements",
    "constraints",
    "context",
    "design_ref",
    "baseline",
    "branch",
)


# Features created before the requirements schema (workflow recursive-issues/v1) keep their
# original definition, and therefore their original digest. They are read, never converted.
LEGACY_DEFINITION = tuple(
    "acceptance_criteria" if key == "requirements" else key for key in DEFINITION
)

REQUIREMENT_FIELDS = ("title", "objective", "acceptance_criteria", "constraints")


def _is_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def definition_digest(feature: dict) -> str:
    """Digest of the immutable feature definition and its identity (including the instance)."""
    if "requirements" in feature:
        scope = {key: feature.get(key) for key in DEFINITION}
        # Identity is the id, so the digest binds the canonical (sorted) set, never input order.
        scope["requirements"] = requirements.canonical(feature["requirements"])
    else:
        scope = {key: feature.get(key) for key in LEGACY_DEFINITION}
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
    text stored. ``context`` and ``design_ref`` are required; ``title``, ``objective`` and
    ``constraints`` may accompany them and are replaced together with the context. A
    requirements-schema feature may also carry ``requirements``: a full explicit replacement of
    the collection (unchanged ids keep their identity, removed ids are dropped, new ids are
    added) that re-derives the root contract in the same step; an id removed by an earlier
    revision cannot be added again. Legacy (v1) features revise ``acceptance_criteria`` instead.
    Refused
    once approved, and once any issue design work exists, because that work was derived from
    the previous text. ``design_ref`` must be the SHA-256 of the new context so the approved
    reference always names the stored design.
    """
    if isinstance(payload, dict) and "requirements" in payload and "requirements" not in feature:
        raise ValueError("Requirements are not supported by legacy (recursive-issues/v1) features")
    if isinstance(payload, dict) and "requirements" in feature and "acceptance_criteria" in payload:
        raise ValueError(
            "Feature acceptance_criteria are replaced by requirements and cannot be revised"
        )
    require(
        isinstance(payload, dict)
        and {"context", "design_ref"} <= set(payload)
        and set(payload) <= {"context", "design_ref", "requirements", *REQUIREMENT_FIELDS}
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
    new_requirements = (
        requirements.canonical(payload["requirements"]) if "requirements" in payload else None
    )
    require(
        "direction" not in feature,
        "Direction is already approved; the design and requirements are frozen "
        "(changing them needs an authorised scope transition, which is not yet supported)",
    )
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
    if new_requirements is not None:
        removed = {
            identifier
            for event in feature["workflow"]["history"]
            for identifier in event.get("removed_requirements", [])
        }
        reused = sorted(removed & {record["id"] for record in new_requirements})
        require(
            not reused,
            f"Requirement id was removed earlier and cannot be reused: {', '.join(reused)}",
        )
    for target in (feature, root):
        target["context"] = payload["context"]
        for field in REQUIREMENT_FIELDS:
            if field in payload:
                target[field] = copy.deepcopy(payload[field])
    event = {"event": "design-revised", "design_ref": expected}
    if new_requirements is not None:
        kept = {record["id"] for record in new_requirements}
        removed_now = sorted(r["id"] for r in feature["requirements"] if r["id"] not in kept)
        if removed_now:
            event["removed_requirements"] = removed_now
        feature["requirements"] = new_requirements
        # The root's feature contract is re-derived, never edited separately.
        root["acceptance_criteria"] = requirements.derived_criteria(new_requirements)
    feature["design_ref"] = expected
    root["origin"] = {"kind": "ticket", "reference": expected}
    feature["workflow"]["history"].append(event)


def require_approved(feature: dict) -> None:
    approval = feature.get("direction")
    require(
        isinstance(approval, dict) and approval.get("scope_digest") == definition_digest(feature),
        "Approve the feature direction before implementation",
    )
