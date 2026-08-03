"""
Repository-state validation helpers for planning benchmarks.

These helpers validate that a produced plan is grounded in the actual state
of the target repository: referenced files exist, symbols are present, and
the plan does not propose architecturally wrong changes.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ...planning.models import PlanningResult
from ..models import GateResult


def check_referenced_paths_exist(
    result: PlanningResult,
    repo_root: Path,
    mentioned_paths: list[str],
) -> GateResult:
    """
    Verify that every file path explicitly mentioned in the plan text
    (as extracted by the caller) exists in the checked-out repository.

    Returns a gate that is INDETERMINATE when no paths were extracted
    (the caller could not identify references) rather than blindly passing.
    """
    if not mentioned_paths:
        return GateResult(
            gate="referenced_paths_exist",
            passed=None,
            critical=False,
            reason="No file paths could be extracted from plan text for validation.",
        )

    missing: list[str] = [p for p in mentioned_paths if not (repo_root / p).exists()]
    if missing:
        return GateResult(
            gate="referenced_paths_exist",
            passed=False,
            critical=True,
            reason=f"Referenced path(s) do not exist in repo: {missing}",
            evidence={"missing_paths": missing},
        )

    return GateResult(
        gate="referenced_paths_exist",
        passed=True,
        critical=True,
        reason=f"All {len(mentioned_paths)} referenced path(s) exist.",
    )


def check_forbidden_paths_absent(
    result: PlanningResult,
    forbidden_paths: list[str],
    plan_text_lower: str,
) -> GateResult:
    """
    Verify that none of the fixture-defined forbidden paths are referenced
    in the plan.
    """
    triggered = [p for p in forbidden_paths if Path(p).name.lower() in plan_text_lower]
    if triggered:
        return GateResult(
            gate="forbidden_paths_absent",
            passed=False,
            critical=True,
            reason=f"Plan references forbidden path(s): {triggered}",
            evidence={"forbidden_paths_mentioned": triggered},
        )
    return GateResult(
        gate="forbidden_paths_absent",
        passed=True,
        critical=True,
        reason="No forbidden paths referenced in plan.",
    )


def check_git_ref_reachable(repo: Path, ref: str) -> GateResult:
    """
    Verify that the fixture's pinned commit ref is reachable in the repo.
    """
    result = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-t", ref],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return GateResult(
            gate="fixture_ref_reachable",
            passed=False,
            critical=True,
            reason=f"Fixture ref {ref!r} not found in repo {repo}: {result.stderr.strip()}",
        )
    return GateResult(
        gate="fixture_ref_reachable",
        passed=True,
        critical=True,
        reason=f"Fixture ref {ref!r} is reachable.",
    )
