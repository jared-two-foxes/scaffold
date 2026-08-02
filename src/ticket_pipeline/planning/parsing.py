from __future__ import annotations

from ..lib import pipeline_lib as lib
from .models import PlannedCriterion, PlanningDiagnostic, PlanningResult


def parse_gap_plan(gap_plan_text: str) -> list[PlannedCriterion]:
    return [
        PlannedCriterion(
            criterion=criterion,
            plan_context=lib.extract_plan_context_for_criterion(criterion, gap_plan_text),
            verification=lib.extract_verification_mode(criterion),
            implementation_strategy=lib.extract_strategy(criterion),
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
