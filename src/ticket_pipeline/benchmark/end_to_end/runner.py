"""
End-to-end pipeline benchmark runner (Phase 6 placeholder).

Combines planning and implementation strategy execution with full
stage attribution (spec §17.3)::

    load ticket fixture
        -> execute planning strategy
        -> grade plan artifact
        -> stop if plan rejected
        -> execute selected implementation strategy
        -> grade final repository outcome
        -> retain stage-specific acceptance results
"""

from __future__ import annotations

from ..models import AcceptanceResult, BenchmarkResult, GateResult
from ..acceptance import build_acceptance_result


def end_to_end_not_implemented() -> AcceptanceResult:
    """
    Placeholder: returns INDETERMINATE until end-to-end runner is wired up.
    """
    gate = GateResult(
        gate="end_to_end_execution",
        passed=None,
        critical=False,
        reason=(
            "End-to-end benchmark runner not yet implemented (Phase 6). "
            "Run 'scaffold benchmark planning' and 'scaffold benchmark implementation' "
            "independently for Phases 2 and 3 results."
        ),
    )
    return build_acceptance_result([gate], grader="end_to_end_placeholder")
