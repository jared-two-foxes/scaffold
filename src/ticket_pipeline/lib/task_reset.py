"""Administrative, task-only state reset service.

This module removes *persisted feature associations*, not Git changes, external
evidence, or agent authority. Callers must explicitly authorize an apply after
showing its plan. It is intentionally not part of the task lifecycle operations.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from ticket_pipeline.lib import state_store

SCHEMA_VERSION = 2


@dataclass(frozen=True)
class AffectedFeature:
    """A feature and the number of task/issue records that will be removed."""

    id: str
    task_count: int


@dataclass(frozen=True)
class ResetPlan:
    """Read-only preview, bound to the exact bytes of the current task store."""

    scope: str
    feature_id: str | None
    affected_features: tuple[AffectedFeature, ...]
    total_tasks: int
    will_change: bool
    state_digest: str


def _validate_scope(feature_id: str | None, all_features: bool) -> None:
    if type(all_features) is not bool or (feature_id is None) != all_features:
        raise ValueError("Specify exactly one of feature_id or all_features=True")
    if feature_id is not None and (
        not isinstance(feature_id, str) or not feature_id.strip()
    ):
        raise ValueError("feature_id must be a nonempty string")


def _read_state(path: Path) -> tuple[dict, str]:
    """Read and validate the task store while the caller owns its lock.

    Fingerprint the on-disk *bytes*, including whether the file is absent:
    preview/confirmation cannot silently apply to a newer revision.
    """
    if path.is_symlink():
        raise ValueError("Refusing symbolic link for Scaffold tasks.json")
    if path.exists():
        if not path.is_file():
            raise ValueError("Scaffold tasks.json must be a regular file")
        raw = path.read_bytes()
        digest = hashlib.sha256(b"present\x00" + raw).hexdigest()
        try:
            state = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("Invalid Scaffold task state JSON") from exc
    else:
        state = {"schema_version": SCHEMA_VERSION, "features": {}}
        digest = hashlib.sha256(b"absent\x00").hexdigest()

    if (
        not isinstance(state, dict)
        or type(state.get("schema_version")) is not int
        or state["schema_version"] != SCHEMA_VERSION
        or not isinstance(state.get("features"), dict)
    ):
        raise ValueError("Unsupported task state schema_version")

    for identifier, feature in state["features"].items():
        if (
            not isinstance(identifier, str)
            or not isinstance(feature, dict)
            or not isinstance(feature.get("tasks"), dict)
        ):
            raise ValueError("Malformed feature in Scaffold task state")

    return state, digest


def _build_plan(
    state: dict, digest: str, feature_id: str | None, all_features: bool
) -> ResetPlan:
    features = state["features"]
    selected = sorted(features) if all_features else (
        [feature_id] if feature_id in features else []
    )
    affected = tuple(
        AffectedFeature(id=identifier, task_count=len(features[identifier]["tasks"]))
        for identifier in selected
    )
    return ResetPlan(
        scope="all" if all_features else "feature",
        feature_id=feature_id,
        affected_features=affected,
        total_tasks=sum(item.task_count for item in affected),
        will_change=bool(affected),
        state_digest=digest,
    )


def plan_reset(*, feature_id: str | None = None, all_features: bool = False) -> ResetPlan:
    """Preview a feature-scoped or whole-store reset, without writing tasks.json.

    The shared state lock may create the existing lock/directory if necessary.
    It is never unlinked, even for a whole-store reset.
    """
    _validate_scope(feature_id, all_features)
    with state_store.state_lock():
        path = state_store.SCAFFOLD_DIR / "tasks.json"
        state, digest = _read_state(path)
        return _build_plan(state, digest, feature_id, all_features)


def apply_reset(plan: ResetPlan) -> ResetPlan:
    """Apply exactly a previously previewed reset, or reject stale/forged plans.

    The comparison and atomic replacement happen within one exclusive lock.
    No-op plans are idempotent and never rewrite an existing task store.
    """
    if not isinstance(plan, ResetPlan) or plan.scope not in {"feature", "all"}:
        raise ValueError("A valid reset plan is required")
    all_features = plan.scope == "all"
    _validate_scope(plan.feature_id, all_features)

    with state_store.state_lock():
        path = state_store.SCAFFOLD_DIR / "tasks.json"
        state, digest = _read_state(path)
        current = _build_plan(state, digest, plan.feature_id, all_features)
        if current != plan:
            raise ValueError("Scaffold task state changed since preview; re-plan and confirm")
        if not current.will_change:
            return current

        if all_features:
            updated = {"schema_version": SCHEMA_VERSION, "features": {}}
        else:
            # Preserve other features, their receipt histories and any
            # non-feature top-level state without re-serializing their shape.
            updated = {**state, "features": dict(state["features"])}
            del updated["features"][plan.feature_id]

        state_store.atomic_write_json(path, updated)
        return current
