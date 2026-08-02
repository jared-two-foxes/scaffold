"""
agent_validation - structural and semantic validation of AgentPlanSubmission.

Validates the raw arguments dict from a submit_plan tool call, converts it
to an AgentPlanSubmission, and checks criterion coverage against the
expected IDs. Returns either a validated submission or a list of concise
error messages to feed back to the model.
"""

from __future__ import annotations

from .agent_models import (
    VALID_DISPOSITIONS,
    AgentAssumption,
    AgentCriterionAssessment,
    AgentEvidence,
    AgentPlanSubmission,
    PlannedChange,
)
from ..planning.models import VALID_VERIFICATION_MODES, VALID_IMPLEMENTATION_STRATEGIES


def _parse_evidence(raw_list: list | None) -> tuple[AgentEvidence, ...]:
    if not raw_list:
        return ()
    result = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        obs = item.get("observation", "")
        if obs:
            result.append(AgentEvidence(path=item.get("path"), observation=obs))
    return tuple(result)


def _parse_planned_changes(raw_list: list | None) -> tuple[PlannedChange, ...]:
    if not raw_list:
        return ()
    result = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        path = item.get("path", "")
        desc = item.get("description", "")
        if path and desc:
            syms = tuple(s for s in item.get("symbols", []) if isinstance(s, str))
            result.append(PlannedChange(path=path, description=desc, symbols=syms))
    return tuple(result)


def _parse_assumptions(raw_list: list | None) -> tuple[AgentAssumption, ...]:
    if not raw_list:
        return ()
    result = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        q = item.get("question", "")
        a = item.get("answer", "")
        b = item.get("basis", "")
        if q and a:
            result.append(AgentAssumption(question=q, answer=a, basis=b))
    return tuple(result)


def validate_submission(
    args: dict,
    expected_criterion_ids: list[str],
) -> tuple[AgentPlanSubmission | None, list[str]]:
    """
    Validate the raw submit_plan arguments dict.

    Returns (submission, errors). If errors is non-empty, submission is None
    and the errors should be returned to the agent for correction.
    """
    errors: list[str] = []

    # --- Required top-level string fields ---
    ticket_summary = args.get("ticket_summary", "")
    approach_summary = args.get("approach_summary", "")
    if not isinstance(ticket_summary, str) or not ticket_summary.strip():
        errors.append("ticket_summary is required and must be a non-empty string.")
    if not isinstance(approach_summary, str) or not approach_summary.strip():
        errors.append("approach_summary is required and must be a non-empty string.")

    # --- Criteria ---
    raw_criteria = args.get("criteria")
    if not isinstance(raw_criteria, list):
        errors.append("criteria must be a list.")
        return None, errors

    seen_ids: set[str] = set()
    parsed_criteria: list[AgentCriterionAssessment] = []

    for i, item in enumerate(raw_criteria):
        if not isinstance(item, dict):
            errors.append(f"criteria[{i}] must be an object.")
            continue

        cid = item.get("criterion_id", "")
        source = item.get("source_criterion", "")
        disposition = item.get("disposition", "")
        rationale = item.get("rationale", "")

        criterion_errors: list[str] = []

        if not cid:
            criterion_errors.append("criterion_id is required.")
        elif cid in seen_ids:
            criterion_errors.append(f"duplicate criterion_id: {cid!r}.")
        elif expected_criterion_ids and cid not in expected_criterion_ids:
            criterion_errors.append(
                f"unknown criterion_id {cid!r}; expected one of {expected_criterion_ids}."
            )

        if not source or not source.strip():
            criterion_errors.append(
                "source_criterion is required and must be non-empty."
            )

        if not disposition:
            criterion_errors.append("disposition is required.")
        elif disposition not in VALID_DISPOSITIONS:
            criterion_errors.append(
                f"disposition {disposition!r} is not valid; expected one of "
                f"{sorted(VALID_DISPOSITIONS)}."
            )

        if not rationale or not rationale.strip():
            criterion_errors.append("rationale is required and must be non-empty.")

        evidence = _parse_evidence(item.get("evidence"))
        planned_changes = _parse_planned_changes(item.get("planned_changes"))
        verification = item.get("verification")
        impl_strategy = item.get("implementation_strategy")
        existing_test_refs = tuple(
            r
            for r in item.get("existing_test_refs", [])
            if isinstance(r, str) and r.strip()
        )
        for ref in existing_test_refs:
            if "::" not in ref:
                criterion_errors.append(
                    f"{cid}: existing_test_refs entries must be in 'file::qualified_test_name' shape (got {ref!r})."
                )
        plan_context = item.get("plan_context")
        blocker = item.get("blocker")

        # Disposition-specific rules
        if disposition == "remaining":
            if not planned_changes:
                criterion_errors.append(
                    f"{cid}: remaining criteria must have at least one planned_change."
                )
            if not verification:
                criterion_errors.append(
                    f"{cid}: remaining criteria must specify verification."
                )
            elif verification not in VALID_VERIFICATION_MODES:
                criterion_errors.append(
                    f"{cid}: verification {verification!r} is not valid; "
                    f"expected one of {sorted(VALID_VERIFICATION_MODES)}."
                )
            if not impl_strategy:
                criterion_errors.append(
                    f"{cid}: remaining criteria must specify implementation_strategy."
                )
            elif impl_strategy not in VALID_IMPLEMENTATION_STRATEGIES:
                criterion_errors.append(
                    f"{cid}: implementation_strategy {impl_strategy!r} is not valid; "
                    f"expected one of {sorted(VALID_IMPLEMENTATION_STRATEGIES)}."
                )

        elif disposition == "satisfied":
            if not evidence:
                criterion_errors.append(
                    f"{cid}: satisfied criteria must have at least one evidence item "
                    "with a concrete repository observation."
                )
            if planned_changes:
                criterion_errors.append(
                    f"{cid}: satisfied criteria must not have planned_changes."
                )

        elif disposition == "not_applicable":
            if planned_changes:
                criterion_errors.append(
                    f"{cid}: not_applicable criteria must not have planned_changes."
                )

        elif disposition == "blocked":
            if not blocker or not blocker.strip():
                criterion_errors.append(
                    f"{cid}: blocked criteria must include a non-empty blocker description."
                )
            if planned_changes:
                criterion_errors.append(
                    f"{cid}: blocked criteria must not have planned_changes "
                    "(do not fabricate a plan when blocked)."
                )

        errors.extend(criterion_errors)

        if not criterion_errors and cid:
            seen_ids.add(cid)
            parsed_criteria.append(
                AgentCriterionAssessment(
                    criterion_id=cid,
                    source_criterion=source,
                    disposition=disposition,  # type: ignore[arg-type]
                    rationale=rationale,
                    evidence=evidence,
                    planned_changes=planned_changes,
                    verification=verification,
                    implementation_strategy=impl_strategy,
                    existing_test_refs=existing_test_refs,
                    plan_context=plan_context,
                    blocker=blocker,
                )
            )

    # --- Coverage check: every expected ID must appear ---
    if expected_criterion_ids:
        missing = [cid for cid in expected_criterion_ids if cid not in seen_ids]
        if missing:
            errors.append(
                f"Missing assessments for criterion(s): {', '.join(missing)}. "
                "Every criterion must have exactly one assessment."
            )

    if errors:
        return None, errors

    submission = AgentPlanSubmission(
        ticket_summary=ticket_summary,
        approach_summary=approach_summary,
        assumptions=_parse_assumptions(args.get("assumptions")),
        repository_findings=_parse_evidence(args.get("repository_findings")),
        criteria=tuple(parsed_criteria),
        cross_cutting_changes=_parse_planned_changes(args.get("cross_cutting_changes")),
        risks=tuple(r for r in args.get("risks", []) if isinstance(r, str)),
        validation_notes=tuple(
            n for n in args.get("validation_notes", []) if isinstance(n, str)
        ),
    )
    return submission, []
