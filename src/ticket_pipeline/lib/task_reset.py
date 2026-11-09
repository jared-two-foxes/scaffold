"""Administrative, task-only feature close.

``scaffold clean`` no longer deletes anything. It appends a ``feature-close`` entry to the
ledger of each selected open feature instance. A closed instance accepts no further work,
a new ``init`` of the same feature id starts a fresh instance, and the closed ledger stays
on disk for audit. This is *not* Git recovery, and it does not authorize continuing any
existing workflow. It is intentionally not part of the task lifecycle operations.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from ticket_pipeline.lib import ledger, state_store

CLOSE_ACTION = "feature-close"


@dataclass(frozen=True)
class AffectedFeature:
    """A feature and the number of task/issue records in the instance being closed."""

    id: str
    task_count: int


@dataclass(frozen=True)
class ResetPlan:
    """Read-only preview, bound to the exact ledger heads of the affected instances."""

    scope: str
    feature_id: str | None
    affected_features: tuple[AffectedFeature, ...]
    total_tasks: int
    will_change: bool
    state_digest: str


def _validate_scope(feature_id: str | None, all_features: bool) -> None:
    if type(all_features) is not bool or (feature_id is None) != all_features:
        raise ValueError("Specify exactly one of feature_id or all_features=True")
    if feature_id is not None:
        ledger.require_feature_id(feature_id)


def _selected(feature_id: str | None, all_features: bool) -> list[ledger.Ledger]:
    if all_features:
        return ledger.all_open()
    current = ledger.open_current(feature_id)
    return [current] if current is not None else []


def _build_plan(
    selected: list[ledger.Ledger], feature_id: str | None, all_features: bool
) -> ResetPlan:
    affected = tuple(
        AffectedFeature(id=led.state["id"], task_count=len(led.state["tasks"])) for led in selected
    )
    # Bind the preview to each instance's chain head: any later write invalidates it.
    heads = [[led.state["id"], led.state["workflow"]["instance_id"], led.head] for led in selected]
    return ResetPlan(
        scope="all" if all_features else "feature",
        feature_id=feature_id,
        affected_features=affected,
        total_tasks=sum(item.task_count for item in affected),
        will_change=bool(affected),
        state_digest=hashlib.sha256(ledger.canonical(heads)).hexdigest(),
    )


def plan_reset(*, feature_id: str | None = None, all_features: bool = False) -> ResetPlan:
    """Preview closing a feature or every open feature, without writing anything.

    The shared state lock may create the existing lock/directory if necessary.
    """
    _validate_scope(feature_id, all_features)
    with state_store.state_lock():
        return _build_plan(_selected(feature_id, all_features), feature_id, all_features)


def apply_reset(plan: ResetPlan) -> ResetPlan:
    """Close exactly the previewed instances, or reject stale/forged plans.

    The comparison and the appends happen within one exclusive lock. Each close is a
    separate append, so an interruption during ``--all`` leaves some instances closed and
    the rest open; running the command again closes the remainder. No-op plans are idempotent.
    """
    if not isinstance(plan, ResetPlan) or plan.scope not in {"feature", "all"}:
        raise ValueError("A valid reset plan is required")
    all_features = plan.scope == "all"
    _validate_scope(plan.feature_id, all_features)

    with state_store.state_lock():
        selected = _selected(plan.feature_id, all_features)
        current = _build_plan(selected, plan.feature_id, all_features)
        if current != plan:
            raise ValueError("Scaffold feature state changed since preview; re-plan and confirm")
        for led in selected:
            closed = {**led.state, "closed": {"reason": "clean"}}
            led.append(CLOSE_ACTION, {"reason": "clean"}, closed)
        return current
