from __future__ import annotations

from typing import Protocol

from .models import PlanningRequest, PlanningResult


class PlanningError(RuntimeError):
    pass


class PlanningStrategy(Protocol):
    name: str

    def plan(self, request: PlanningRequest) -> PlanningResult: ...
