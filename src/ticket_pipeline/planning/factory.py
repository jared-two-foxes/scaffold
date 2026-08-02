from __future__ import annotations

from .strategies import AgentPlanningStrategy, MechanicalPlanningStrategy
from .strategy import PlanningError, PlanningStrategy

SUPPORTED_PLANNING_STRATEGIES = ("mechanical", "agent")


def validate_planning_strategy_name(name: str) -> str:
    normalized = name.strip().lower()
    if normalized not in SUPPORTED_PLANNING_STRATEGIES:
        raise PlanningError(
            f"Unknown planning strategy {name!r}.\n"
            f"Supported strategies: {', '.join(SUPPORTED_PLANNING_STRATEGIES)}."
        )
    return normalized


def create_planning_strategy(name: str) -> PlanningStrategy:
    normalized = validate_planning_strategy_name(name)
    if normalized == "mechanical":
        return MechanicalPlanningStrategy()
    return AgentPlanningStrategy()
