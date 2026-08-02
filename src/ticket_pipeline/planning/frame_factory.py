from __future__ import annotations

from ..lib import pipeline_lib as lib
from .models import PlanningResult


def build_ticket_frames(
    *,
    ticket_id: str,
    ticket_content: str | None,
    planning_result: PlanningResult,
    strategy_override: str | None = None,
) -> list[lib.CriterionFrame]:
    return [
        lib.CriterionFrame(
            ticket=ticket_id,
            criterion=item.criterion,
            plan_context=item.plan_context,
            test_files=None,
            test_names=None,
            status="pending",
            origin="ticket",
            verification=item.verification,
            strategy=strategy_override or item.implementation_strategy,
            existing_test_refs=list(item.existing_test_refs),
            ticket_snapshot=ticket_content,
        )
        for item in planning_result.criteria
    ]
