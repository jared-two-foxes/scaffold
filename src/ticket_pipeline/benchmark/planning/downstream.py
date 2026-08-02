"""
Downstream planning evaluation.

Runs a fixed downstream implementation executor against a plan produced
by the planning strategy under test (spec §7.7).

This produces two distinct results:

- ``artifact_acceptance``:   whether the plan artifact itself is valid.
- ``downstream_acceptance``: whether a downstream executor could use it.

These must not be collapsed into a single unexplained result.
"""

from __future__ import annotations

from ..models import AcceptanceResult, GateResult
from ..acceptance import build_acceptance_result


def grade_downstream_not_implemented() -> AcceptanceResult:
    """
    Placeholder gate for downstream evaluation.  Returns INDETERMINATE
    until a downstream executor is wired in.
    """
    gate = GateResult(
        gate="downstream_execution",
        passed=None,
        critical=False,
        reason=(
            "Downstream implementation executor not yet configured for this fixture. "
            "Result is INDETERMINATE until a downstream runner is provided."
        ),
    )
    return build_acceptance_result([gate], grader="downstream_placeholder")
