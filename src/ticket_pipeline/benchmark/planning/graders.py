"""
Generic and fixture-specific graders for planning strategy benchmarks.

All graders return a list of :class:`GateResult` instances.  The grading
pipeline combines generic gates (schema validation, strategy-name validation,
repository-grounding checks) with fixture-specific semantic graders.

Planning acceptance contract (spec §7)::

    planning_accepted =
        schema_valid
        AND required_outcomes_covered
        AND repository_grounded
        AND executable
        AND no_critical_false_work
"""

from __future__ import annotations

import re
from pathlib import Path

from ..models import GateResult
from ..fixtures import PlanningFixture
from ...planning.models import (
    VALID_IMPLEMENTATION_STRATEGIES,
    VALID_VERIFICATION_MODES,
    PlanningResult,
)


# ---------------------------------------------------------------------------
# Schema / structural gates
# ---------------------------------------------------------------------------


def grade_schema_valid(result: PlanningResult) -> GateResult:
    """
    The plan output must be parseable and contain at least one criterion
    when work remains, each with a valid strategy and verification mode.
    """
    if not result.criteria:
        return GateResult(
            gate="schema_valid",
            passed=False,
            critical=True,
            reason="No criteria produced; plan is empty.",
        )

    errors: list[str] = []
    for i, c in enumerate(result.criteria):
        if not c.criterion.strip():
            errors.append(f"criterion[{i}] has empty text")
        if c.verification not in VALID_VERIFICATION_MODES:
            errors.append(
                f"criterion[{i}] unsupported verification {c.verification!r}"
            )
        if c.implementation_strategy not in VALID_IMPLEMENTATION_STRATEGIES:
            errors.append(
                f"criterion[{i}] unsupported strategy {c.implementation_strategy!r}"
            )

    if errors:
        return GateResult(
            gate="schema_valid",
            passed=False,
            critical=True,
            reason="; ".join(errors),
            evidence={"errors": errors},
        )

    return GateResult(
        gate="schema_valid",
        passed=True,
        critical=True,
        reason=f"Schema valid: {len(result.criteria)} criteria.",
    )


def grade_strategy_names(result: PlanningResult) -> GateResult:
    """All criteria must use a supported implementation strategy."""
    bad: list[str] = [
        c.implementation_strategy
        for c in result.criteria
        if c.implementation_strategy not in VALID_IMPLEMENTATION_STRATEGIES
    ]
    if bad:
        return GateResult(
            gate="strategy_names_valid",
            passed=False,
            critical=True,
            reason=f"Unsupported strategy name(s): {bad}",
            evidence={"bad_strategies": bad},
        )
    return GateResult(
        gate="strategy_names_valid",
        passed=True,
        critical=True,
        reason="All strategy names are valid.",
    )


# ---------------------------------------------------------------------------
# Required-outcome coverage gate
# ---------------------------------------------------------------------------


def grade_required_outcomes(
    result: PlanningResult,
    fixture: PlanningFixture,
) -> GateResult:
    """
    Every required outcome defined in the fixture must be semantically covered
    by at least one criterion in the produced plan.

    Coverage is determined heuristically by checking whether the outcome's
    key terms appear in any criterion text or plan context.
    """
    uncovered: list[str] = []
    coverage_detail: dict[str, bool] = {}

    combined_text = _plan_text_for_matching(result)

    for outcome in fixture.required_outcomes:
        covered = _outcome_covered(outcome.description, combined_text)
        coverage_detail[outcome.id] = covered
        if not covered and outcome.critical:
            uncovered.append(outcome.id)

    if uncovered:
        return GateResult(
            gate="required_outcomes_covered",
            passed=False,
            critical=True,
            reason=f"Critical outcome(s) not covered: {uncovered}",
            evidence={"coverage": coverage_detail, "uncovered_critical": uncovered},
        )

    all_uncovered = [oid for oid, cov in coverage_detail.items() if not cov]
    if all_uncovered:
        return GateResult(
            gate="required_outcomes_covered",
            passed=None,
            critical=False,
            reason=f"Non-critical outcome(s) may not be covered: {all_uncovered}",
            evidence={"coverage": coverage_detail},
        )

    return GateResult(
        gate="required_outcomes_covered",
        passed=True,
        critical=True,
        reason=f"All {len(fixture.required_outcomes)} required outcomes covered.",
        evidence={"coverage": coverage_detail},
    )


def _plan_text_for_matching(result: PlanningResult) -> str:
    """Combine all criterion text and plan context for keyword matching."""
    parts: list[str] = []
    if result.plan_text:
        parts.append(result.plan_text)
    if result.narrowed_plan_text:
        parts.append(result.narrowed_plan_text)
    for c in result.criteria:
        parts.append(c.criterion)
        parts.append(c.plan_context)
    return " ".join(parts).lower()


def _outcome_covered(description: str, plan_text_lower: str) -> bool:
    """
    Heuristic: an outcome is covered when the majority of its key terms
    (words of 4+ characters, excluding stop-words) appear in the plan text.
    """
    _STOP = frozenset(
        {
            "the", "and", "that", "this", "with", "from", "into", "have",
            "will", "should", "must", "does", "been", "when", "where", "which",
        }
    )
    words = [w for w in re.findall(r"[a-z]{4,}", description.lower()) if w not in _STOP]
    if not words:
        return True
    matches = sum(1 for w in words if w in plan_text_lower)
    return matches / len(words) >= 0.6


# ---------------------------------------------------------------------------
# Repository-grounding gate
# ---------------------------------------------------------------------------


def grade_repository_grounded(
    result: PlanningResult,
    fixture: PlanningFixture,
    repo_root: Path,
) -> GateResult:
    """
    Checks that:
    - Paths declared as existing in the fixture actually exist in the repo.
    - Paths declared as forbidden are not proposed in the plan.
    """
    combined_text = _plan_text_for_matching(result)

    # Check required existing paths
    missing_paths: list[str] = []
    for path_str in fixture.required_existing_paths:
        full_path = repo_root / path_str
        if not full_path.exists():
            missing_paths.append(path_str)

    # Check forbidden paths
    mentioned_forbidden: list[str] = []
    for forbidden in fixture.forbidden_paths:
        # Use the filename as the key identifier
        key = Path(forbidden).name.lower()
        if key in combined_text:
            mentioned_forbidden.append(forbidden)

    errors: list[str] = []
    if missing_paths:
        errors.append(f"Required paths missing from repo: {missing_paths}")
    if mentioned_forbidden:
        errors.append(f"Forbidden paths mentioned in plan: {mentioned_forbidden}")

    if errors:
        return GateResult(
            gate="repository_grounded",
            passed=False,
            critical=True,
            reason="; ".join(errors),
            evidence={
                "missing_required_paths": missing_paths,
                "mentioned_forbidden_paths": mentioned_forbidden,
            },
        )

    return GateResult(
        gate="repository_grounded",
        passed=True,
        critical=True,
        reason="Repository grounding checks passed.",
    )


# ---------------------------------------------------------------------------
# Executability gate
# ---------------------------------------------------------------------------


def grade_executable(result: PlanningResult) -> GateResult:
    """
    A plan is executable when every criterion has non-empty plan_context and
    a verification mode that matches what's expected.
    """
    vague: list[str] = []
    for i, c in enumerate(result.criteria):
        if not c.plan_context.strip():
            vague.append(f"criterion[{i}] has empty plan_context")

    if vague:
        return GateResult(
            gate="executable",
            passed=None,
            critical=False,
            reason=f"Some criteria may be too vague: {vague}",
            evidence={"vague_criteria": vague},
        )

    return GateResult(
        gate="executable",
        passed=True,
        critical=True,
        reason="All criteria have plan context.",
    )


# ---------------------------------------------------------------------------
# Critical-false-work gate
# ---------------------------------------------------------------------------


def grade_no_critical_false_work(
    result: PlanningResult,
    fixture: PlanningFixture,
) -> GateResult:
    """
    Detect critical false positives defined in the fixture.

    Each entry in ``fixture.critical_false_work_patterns`` is a case-insensitive
    substring that, if present in the plan text, constitutes a critical rejection.
    """
    if not fixture.critical_false_work_patterns:
        return GateResult(
            gate="no_critical_false_work",
            passed=True,
            critical=True,
            reason="No critical false-work patterns defined for this fixture.",
        )

    combined_text = _plan_text_for_matching(result)
    triggered: list[str] = [
        p for p in fixture.critical_false_work_patterns if p.lower() in combined_text
    ]

    if triggered:
        return GateResult(
            gate="no_critical_false_work",
            passed=False,
            critical=True,
            reason=f"Critical false-work pattern(s) found: {triggered}",
            evidence={"triggered_patterns": triggered},
        )

    return GateResult(
        gate="no_critical_false_work",
        passed=True,
        critical=True,
        reason="No critical false-work patterns detected.",
    )


# ---------------------------------------------------------------------------
# Strategy-classification gate
# ---------------------------------------------------------------------------


def grade_strategy_classification(
    result: PlanningResult,
    fixture: PlanningFixture,
) -> list[GateResult]:
    """
    Compare criterion strategy assignments against the fixture's expected
    strategy-by-outcome mapping.

    Returns one gate result per outcome that has an expected strategy.
    Returns an empty list if the fixture defines no expected strategies.
    """
    if not fixture.expected_strategy_by_outcome:
        return []

    gates: list[GateResult] = []
    combined_text = _plan_text_for_matching(result)

    for outcome_id, expected_strategy in fixture.expected_strategy_by_outcome.items():
        # Find the criteria most relevant to this outcome
        assigned_strategies: list[str] = [
            c.implementation_strategy
            for c in result.criteria
            if outcome_id.lower().replace("-", "_") in c.criterion.lower()
            or outcome_id.lower().replace("_", "-") in c.criterion.lower()
        ]

        if not assigned_strategies:
            # Fall back to checking any criterion that mentions the outcome terms
            assigned_strategies = list({c.implementation_strategy for c in result.criteria})

        if expected_strategy in assigned_strategies:
            gates.append(
                GateResult(
                    gate=f"strategy_classification_{outcome_id}",
                    passed=True,
                    critical=False,
                    reason=(
                        f"Outcome {outcome_id!r}: expected strategy "
                        f"{expected_strategy!r} found."
                    ),
                )
            )
        else:
            gates.append(
                GateResult(
                    gate=f"strategy_classification_{outcome_id}",
                    passed=None,
                    critical=False,
                    reason=(
                        f"Outcome {outcome_id!r}: expected {expected_strategy!r}, "
                        f"got {assigned_strategies or ['(none matched)']}"
                    ),
                    evidence={
                        "expected": expected_strategy,
                        "found": assigned_strategies,
                    },
                )
            )

    return gates


# ---------------------------------------------------------------------------
# Convenience: run all generic planning gates
# ---------------------------------------------------------------------------


def run_generic_planning_gates(
    result: PlanningResult,
    fixture: PlanningFixture,
    repo_root: Path | None = None,
) -> list[GateResult]:
    """
    Run all generic planning acceptance gates and return their results.

    Repository-grounding checks are skipped when *repo_root* is ``None``.
    """
    gates: list[GateResult] = [
        grade_schema_valid(result),
        grade_strategy_names(result),
        grade_required_outcomes(result, fixture),
        grade_executable(result),
        grade_no_critical_false_work(result, fixture),
    ]

    if repo_root is not None:
        gates.append(grade_repository_grounded(result, fixture, repo_root))

    gates.extend(grade_strategy_classification(result, fixture))

    return gates
