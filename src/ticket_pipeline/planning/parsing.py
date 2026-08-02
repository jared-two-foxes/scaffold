from __future__ import annotations

from ..lib import pipeline_lib as lib
from .models import (
    VALID_IMPLEMENTATION_STRATEGIES,
    VALID_VERIFICATION_MODES,
    PlannedCriterion,
    PlanningDiagnostic,
    PlanningResult,
)
from .strategy import PlanningError


def _require_verification(criterion: str) -> str:
    mode = lib.extract_verification_mode(criterion)
    if mode is None:
        raise PlanningError(
            f"Criterion is missing an explicit 'verify:' tag and cannot be "
            f"parsed without one. Add a verify tag (one of: "
            f"{sorted(VALID_VERIFICATION_MODES)}) to the criterion's "
            f"trailing HTML comment.\nCriterion: {criterion!r}"
        )
    return mode


def _require_strategy(criterion: str) -> str:
    strategy = lib.extract_strategy(criterion)
    if strategy is None:
        raise PlanningError(
            f"Criterion is missing an explicit 'strategy:' tag and cannot be "
            f"parsed without one. Add a strategy tag (one of: "
            f"{sorted(VALID_IMPLEMENTATION_STRATEGIES)}) to the criterion's "
            f"trailing HTML comment.\nCriterion: {criterion!r}"
        )
    return strategy


def parse_gap_plan(gap_plan_text: str) -> list[PlannedCriterion]:
    return [
        PlannedCriterion(
            criterion=criterion,
            plan_context=lib.extract_plan_context_for_criterion(criterion, gap_plan_text),
            verification=_require_verification(criterion),
            implementation_strategy=_require_strategy(criterion),
            existing_test_refs=tuple(lib.extract_existing_test_refs(criterion)),
        )
        for criterion in lib.extract_acceptance_criteria(gap_plan_text)
    ]


def planning_result_from_gap_plan(gap_plan_text: str) -> PlanningResult:
    criteria = parse_gap_plan(gap_plan_text)
    diagnostics: list[PlanningDiagnostic] = []
    for item in criteria:
        if not item.plan_context.strip():
            diagnostics.append(
                PlanningDiagnostic(
                    level="warning",
                    code="missing_plan_context",
                    message=f"Criterion has empty plan context: {item.criterion}",
                )
            )
    return PlanningResult(
        criteria=tuple(criteria),
        plan_text=None,
        narrowed_plan_text=gap_plan_text,
        diagnostics=tuple(diagnostics),
    )
