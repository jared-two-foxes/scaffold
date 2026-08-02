from __future__ import annotations

from ..models import PlanningRequest, PlanningResult
from ..strategy import PlanningError


class AgentPlanningStrategy:
    name = "agent"

    def plan(self, request: PlanningRequest) -> PlanningResult:
        raise PlanningError(
            "Planning strategy 'agent' is not implemented in this version.\n"
            "Use '--planning-strategy mechanical'."
        )
