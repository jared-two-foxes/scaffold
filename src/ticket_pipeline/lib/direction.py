"""Feature direction approval, recorded in the ledger.

The user's one approval of a feature's direction is a ledger entry bound to a digest of the
feature definition. Scaffold computes that digest itself, so a caller cannot approve a stale
or different definition. Implementation (``start``) is refused until the approval matches.
This records *that* approval was supplied; the native Pi tool is what obtains it from the user.
"""

from __future__ import annotations

from ticket_pipeline.lib import ledger
from ticket_pipeline.lib.refs import require

ACTIONS = {"direction-approve"}
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


def require_approved(feature: dict) -> None:
    approval = feature.get("direction")
    require(
        isinstance(approval, dict) and approval.get("scope_digest") == definition_digest(feature),
        "Approve the feature direction before implementation",
    )
