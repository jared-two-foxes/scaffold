from __future__ import annotations

from ...lib import pipeline_lib as lib
from ..models import PlanningRequest, PlanningResult
from ..parsing import planning_result_from_gap_plan
from ..strategy import PlanningError


class MechanicalPlanningStrategy:
    name = "mechanical"

    def plan(self, request: PlanningRequest) -> PlanningResult:
        lib.remove_scratch_files((lib.TICKET_FILE, lib.PLAN_FILE, lib.GAP_PLAN_FILE))
        lib.TICKET_FILE.write_text(request.ticket_content, encoding="utf-8")
        lib.walk(
            lib.build_planning_blocks(
                ticket_id=request.ticket_id,
                model=request.model,
                step_models=request.step_models,
                ticket_file_in=lib.TICKET_FILE,
            )
        )
        plan_text = lib.PLAN_FILE.read_text(encoding="utf-8")
        gap_plan_text = lib.GAP_PLAN_FILE.read_text(encoding="utf-8")
        try:
            result = planning_result_from_gap_plan(gap_plan_text)
        except ValueError as exc:
            raise PlanningError(f"Planning strategy 'mechanical' returned invalid data: {exc}") from exc
        return PlanningResult(
            criteria=result.criteria,
            plan_text=plan_text,
            narrowed_plan_text=gap_plan_text,
            diagnostics=result.diagnostics,
        )
