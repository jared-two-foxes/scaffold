from __future__ import annotations

import logging
import os

from ...lib import pipeline_lib as lib
from ...lib.ai_client import AIError, StepBudgetExceeded
from ..agent_models import AgentPlanSubmission
from ..agent_prompt import assign_criterion_ids, build_agent_plan_prompt, extract_acceptance_criteria
from ..agent_rendering import (
    build_agent_diagnostics,
    render_agent_full_plan,
    render_agent_gap_plan,
    render_plan_context,
)
from ..agent_runner import (
    PlanningInputRequired,
    make_read_only_executor,
    run_agent_until_terminal,
)
from ..agent_tools import (
    AGENT_PLANNING_TOOLS,
    PLANNING_FAILED_TOOL_NAME,
    SUBMIT_PLAN_TOOL_NAME,
    TERMINAL_TOOL_NAMES,
)
from ..agent_validation import validate_submission
from ..models import PlannedCriterion, PlanningRequest, PlanningResult
from ..strategy import PlanningError

log = logging.getLogger(__name__)

DEFAULT_MAX_TURNS = 40
DEFAULT_MAX_INVALID_SUBMISSIONS = 2

# Configuration key for agent settings in .dev-pipeline.toml
_AGENT_MODEL_STEP_KEY = "agent_plan"

_VALID_VERIFICATION_MODES = frozenset({"test", "test-refactor", "refactor", "manual"})
_VALID_IMPLEMENTATION_STRATEGIES = frozenset({"tdd", "direct", "manual", "refactor"})


def _require_verification_for_agent(assessment: "AgentCriterionAssessment") -> str:
    """Require an explicit verification value from an agent assessment."""
    from ..agent_models import AgentCriterionAssessment  # noqa: F401
    value = assessment.verification
    if not value:
        raise PlanningError(
            f"Agent submitted a 'remaining' criterion without an explicit "
            f"verification value. Every remaining criterion must declare "
            f"verification (one of: {sorted(_VALID_VERIFICATION_MODES)}). "
            f"Criterion: {assessment.source_criterion!r}"
        )
    return value


def _require_strategy_for_agent(assessment: "AgentCriterionAssessment") -> str:
    """Require an explicit implementation_strategy value from an agent assessment."""
    from ..agent_models import AgentCriterionAssessment  # noqa: F401
    value = assessment.implementation_strategy
    if not value:
        raise PlanningError(
            f"Agent submitted a 'remaining' criterion without an explicit "
            f"implementation_strategy value. Every remaining criterion must "
            f"declare a strategy (one of: {sorted(_VALID_IMPLEMENTATION_STRATEGIES)}). "
            f"Criterion: {assessment.source_criterion!r}"
        )
    return value


class AgentPlanningStrategy:
    name = "agent"

    def __init__(
        self,
        user_input_mode: str = "interactive",
        max_turns: int = DEFAULT_MAX_TURNS,
        max_invalid_submissions: int = DEFAULT_MAX_INVALID_SUBMISSIONS,
    ) -> None:
        if user_input_mode not in {"interactive", "infer", "fail"}:
            raise ValueError(
                f"user_input_mode must be 'interactive', 'infer', or 'fail', "
                f"got {user_input_mode!r}"
            )
        self.user_input_mode = user_input_mode
        self.max_turns = max_turns
        self.max_invalid_submissions = max_invalid_submissions

    def plan(self, request: PlanningRequest) -> PlanningResult:
        log.info("-- Agent planning strategy: starting session for %s", request.ticket_id)

        # Write ticket snapshot
        lib.TICKET_FILE.write_text(request.ticket_content, encoding="utf-8")

        # Resolve model: check step_models for agent_plan key, fall back to request.model
        model = request.step_models.get(_AGENT_MODEL_STEP_KEY, request.model)

        # Extract and assign criterion IDs deterministically
        raw_criteria = extract_acceptance_criteria(request.ticket_content)
        criterion_ids = assign_criterion_ids(raw_criteria)
        expected_ids = [cid for cid, _ in criterion_ids]

        # Build initial prompt
        prompt = build_agent_plan_prompt(request, criterion_ids)

        # Build read-only executor
        executor = make_read_only_executor(
            user_input_mode=self.user_input_mode,
            preloaded_paths={str(lib.TICKET_FILE)},
        )

        # Run the agent loop
        submission = self._run_planning_loop(
            prompt=prompt,
            executor=executor,
            expected_ids=expected_ids,
            model=model,
            label=f"agent-plan({request.ticket_id})",
        )

        # Write artifacts
        plan_text = render_agent_full_plan(submission)
        gap_plan_text = render_agent_gap_plan(submission)
        lib.PLAN_FILE.write_text(plan_text, encoding="utf-8")
        lib.GAP_PLAN_FILE.write_text(gap_plan_text, encoding="utf-8")

        # Log summary
        remaining = sum(1 for a in submission.criteria if a.disposition == "remaining")
        satisfied = sum(1 for a in submission.criteria if a.disposition == "satisfied")
        log.info(
            "-- Agent planning complete:\n"
            "   %d ticket criteria assessed\n"
            "   %d remaining\n"
            "   %d already satisfied",
            len(submission.criteria),
            remaining,
            satisfied,
        )

        # Convert to PlanningResult
        return self._to_planning_result(submission, plan_text, gap_plan_text)

    def _run_planning_loop(
        self,
        prompt: str,
        executor,
        expected_ids: list[str],
        model: str,
        label: str,
    ) -> AgentPlanSubmission:
        """
        Run the agent loop, handle submit_plan validation with retry,
        and handle planning_failed with a structured error.
        """
        invalid_attempts = 0

        # We may need to inject validation errors back into the same context.
        # To support this we run the loop in a way that allows re-entry with
        # an amended prompt including error feedback.
        current_prompt = prompt

        while True:
            try:
                result = run_agent_until_terminal(
                    prompt=current_prompt,
                    tools=AGENT_PLANNING_TOOLS,
                    executor=executor,
                    terminal_tools=TERMINAL_TOOL_NAMES,
                    model=model,
                    max_turns=self.max_turns,
                    label=label,
                )
            except (AIError, StepBudgetExceeded) as exc:
                raise PlanningError(
                    f"Agent planning session failed: {exc}"
                ) from exc

            if result.tool_name == PLANNING_FAILED_TOOL_NAME:
                args = result.arguments
                raise PlanningError(
                    f"Agent planning failed "
                    f"(category={args.get('category', 'other')}, "
                    f"recoverable={args.get('recoverable', False)}): "
                    f"{args.get('reason', '(no reason provided)')}\n"
                    f"Suggested action: {args.get('suggested_action', '(none)')}"
                )

            # submit_plan - validate the payload
            submission, errors = validate_submission(
                result.arguments, expected_ids
            )
            if submission is not None:
                return submission

            invalid_attempts += 1
            if invalid_attempts >= self.max_invalid_submissions:
                error_summary = "; ".join(errors)
                raise PlanningError(
                    f"Agent planning produced {invalid_attempts} invalid submission(s). "
                    f"Last validation errors: {error_summary}"
                )

            # Feed errors back to the same session by appending to the prompt
            error_msg = (
                "Your submit_plan call had validation errors. Please correct them "
                "and call submit_plan again:\n\n"
                + "\n".join(f"- {e}" for e in errors)
            )
            log.warning(
                "   %s: invalid submission (attempt %d/%d) - returning errors to agent.",
                label,
                invalid_attempts,
                self.max_invalid_submissions,
            )
            # For retry, we re-run the entire loop with the error appended.
            # The agent runner itself is stateless - we prepend correction context.
            current_prompt = prompt + "\n\n---\n\n" + error_msg

    def _to_planning_result(
        self,
        submission: AgentPlanSubmission,
        plan_text: str,
        gap_plan_text: str,
    ) -> PlanningResult:
        """Convert AgentPlanSubmission to PlanningResult."""
        remaining_criteria = tuple(
            PlannedCriterion(
                criterion=assessment.source_criterion,
                plan_context=render_plan_context(assessment),
                verification=_require_verification_for_agent(assessment),
                implementation_strategy=_require_strategy_for_agent(assessment),
                existing_test_refs=assessment.existing_test_refs,
            )
            for assessment in submission.criteria
            if assessment.disposition == "remaining"
        )

        return PlanningResult(
            criteria=remaining_criteria,
            plan_text=plan_text,
            narrowed_plan_text=gap_plan_text,
            diagnostics=build_agent_diagnostics(submission),
        )
