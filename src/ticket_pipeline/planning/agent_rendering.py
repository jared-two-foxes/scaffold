"""
agent_rendering - deterministic artifact and plan-context rendering for
AgentPlanningStrategy.

Produces:
  render_agent_full_plan()   → .implementation-plan.md content
  render_agent_gap_plan()    → .gap-plan.md content
  render_plan_context()      → per-criterion plan_context string
  build_agent_diagnostics()  → tuple[PlanningDiagnostic, ...]
"""

from __future__ import annotations

from .agent_models import AgentCriterionAssessment, AgentPlanSubmission
from .models import PlanningDiagnostic


def render_plan_context(assessment: AgentCriterionAssessment) -> str:
    """
    Build a deterministic plan_context string from an AgentCriterionAssessment.
    Used when constructing PlannedCriterion for remaining criteria.
    No downstream component should parse this for metadata.
    """
    parts: list[str] = []

    if assessment.plan_context:
        parts.append(assessment.plan_context.strip())

    if assessment.rationale:
        parts.append(f"Rationale: {assessment.rationale.strip()}")

    if assessment.planned_changes:
        changes = "\n".join(
            f"- {c.path}: {c.description}" + (f" [{', '.join(c.symbols)}]" if c.symbols else "")
            for c in assessment.planned_changes
        )
        parts.append(f"Planned changes:\n{changes}")

    if assessment.evidence:
        evidence = "\n".join(
            f"- {('`' + e.path + '`') if e.path else '(repo)'}: {e.observation}"
            for e in assessment.evidence
        )
        parts.append(f"Evidence:\n{evidence}")

    if assessment.existing_test_refs:
        refs = ", ".join(assessment.existing_test_refs)
        parts.append(f"Existing tests: {refs}")

    if assessment.verification:
        parts.append(f"Verification: {assessment.verification}")

    if assessment.implementation_strategy:
        parts.append(f"Implementation strategy: {assessment.implementation_strategy}")

    return "\n\n".join(parts)


def _render_evidence_block(evidence: tuple) -> str:
    if not evidence:
        return "_None recorded._"
    return "\n".join(
        f"- {('`' + e.path + '`') if e.path else '(no path)'}: {e.observation}" for e in evidence
    )


def _render_assumptions_block(assumptions: tuple) -> str:
    if not assumptions:
        return "_None recorded._"
    lines = []
    for a in assumptions:
        lines.append(f"**Q:** {a.question}")
        lines.append(f"**A:** {a.answer}")
        if a.basis:
            lines.append(f"**Basis:** {a.basis}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _render_criterion_assessment(assessment: AgentCriterionAssessment) -> str:
    lines = [
        f"### {assessment.criterion_id}: {assessment.source_criterion.strip()}",
        "",
        f"**Disposition:** {assessment.disposition}",
        f"**Rationale:** {assessment.rationale}",
    ]

    if assessment.evidence:
        lines.append("")
        lines.append("**Evidence:**")
        for e in assessment.evidence:
            path_part = f"`{e.path}` – " if e.path else ""
            lines.append(f"- {path_part}{e.observation}")

    if assessment.planned_changes:
        lines.append("")
        lines.append("**Planned changes:**")
        for c in assessment.planned_changes:
            sym_part = f" `{', '.join(c.symbols)}`" if c.symbols else ""
            lines.append(f"- `{c.path}`{sym_part}: {c.description}")

    if assessment.verification:
        lines.append(f"**Verification:** {assessment.verification}")
    if assessment.implementation_strategy:
        lines.append(f"**Implementation strategy:** {assessment.implementation_strategy}")
    if assessment.existing_test_refs:
        lines.append(f"**Existing tests:** {', '.join(assessment.existing_test_refs)}")
    if assessment.blocker:
        lines.append(f"**Blocker:** {assessment.blocker}")
    if assessment.plan_context:
        lines.append("")
        lines.append("**Context:**")
        lines.append(assessment.plan_context.strip())

    return "\n".join(lines)


def render_agent_full_plan(submission: AgentPlanSubmission) -> str:
    """
    Render the complete agent planning report as .implementation-plan.md content.
    Deterministic - same submission always produces the same output.
    """
    sections: list[str] = ["# Agent Planning Report", ""]

    sections.append("## Ticket Summary")
    sections.append("")
    sections.append(submission.ticket_summary)
    sections.append("")

    sections.append("## Approach")
    sections.append("")
    sections.append(submission.approach_summary)
    sections.append("")

    sections.append("## Repository Findings")
    sections.append("")
    sections.append(_render_evidence_block(submission.repository_findings))
    sections.append("")

    sections.append("## Assumptions")
    sections.append("")
    sections.append(_render_assumptions_block(submission.assumptions))
    sections.append("")

    sections.append("## Criterion Assessments")
    sections.append("")
    for assessment in submission.criteria:
        sections.append(_render_criterion_assessment(assessment))
        sections.append("")

    if submission.cross_cutting_changes:
        sections.append("## Cross-Cutting Changes")
        sections.append("")
        for c in submission.cross_cutting_changes:
            sym_part = f" `{', '.join(c.symbols)}`" if c.symbols else ""
            sections.append(f"- `{c.path}`{sym_part}: {c.description}")
        sections.append("")

    if submission.risks:
        sections.append("## Risks")
        sections.append("")
        for r in submission.risks:
            sections.append(f"- {r}")
        sections.append("")

    remaining = [a for a in submission.criteria if a.disposition == "remaining"]
    if remaining:
        sections.append("## Implementation Plan")
        sections.append("")
        for assessment in remaining:
            sections.append(f"### {assessment.criterion_id}")
            sections.append("")
            if assessment.planned_changes:
                for c in assessment.planned_changes:
                    sym_part = f" `{', '.join(c.symbols)}`" if c.symbols else ""
                    sections.append(f"- `{c.path}`{sym_part}: {c.description}")
            if assessment.plan_context:
                sections.append("")
                sections.append(assessment.plan_context.strip())
            sections.append("")

        sections.append("## Verification Plan")
        sections.append("")
        for assessment in remaining:
            verification = assessment.verification or "test"
            sections.append(
                f"- {assessment.criterion_id}: {verification}"
                + (
                    f" (existing: {', '.join(assessment.existing_test_refs)})"
                    if assessment.existing_test_refs
                    else ""
                )
            )
        sections.append("")

    return "\n".join(sections)


def render_agent_gap_plan(submission: AgentPlanSubmission) -> str:
    """
    Render the gap plan (.gap-plan.md) containing only remaining criteria.
    Format is compatible with the existing gap-plan parser so that
    --from-gap-plan continues to work.
    """
    remaining = [a for a in submission.criteria if a.disposition == "remaining"]

    lines: list[str] = [
        "<!-- generated by AgentPlanningStrategy -->",
        "",
        "## Implementation Plan",
        "",
    ]

    for assessment in remaining:
        lines.append(f"### {assessment.criterion_id}: {assessment.source_criterion.strip()}")
        if assessment.plan_context:
            lines.append("")
            lines.append(assessment.plan_context.strip())
        if assessment.planned_changes:
            lines.append("")
            for c in assessment.planned_changes:
                lines.append(f"- `{c.path}`: {c.description}")
        lines.append("")

    lines.append("## Acceptance Criteria")
    lines.append("")
    for assessment in remaining:
        verification = assessment.verification or "test"
        impl = assessment.implementation_strategy or "tdd"
        refs_part = ""
        if assessment.existing_test_refs:
            refs_part = "; ".join(f"existing_test: {r}" for r in assessment.existing_test_refs)
            refs_part = f"; {refs_part}"

        lines.append(
            f"- [ ] {assessment.source_criterion.strip()} "
            f"<!-- why: agent-assessed; verify: {verification}; "
            f"strategy: {impl}{refs_part} -->"
        )

    lines.append("")
    return "\n".join(lines)


def build_agent_diagnostics(
    submission: AgentPlanSubmission,
) -> tuple[PlanningDiagnostic, ...]:
    """
    Build PlanningDiagnostic entries from the submission.
    Assumptions become info-level diagnostics for auditability.
    """
    diagnostics: list[PlanningDiagnostic] = []

    for assumption in submission.assumptions:
        diagnostics.append(
            PlanningDiagnostic(
                level="info",
                code="agent_assumption",
                message=f"Assumption: Q={assumption.question!r} A={assumption.answer!r}",
            )
        )

    for assessment in submission.criteria:
        if assessment.disposition == "remaining" and not (assessment.plan_context or "").strip():
            diagnostics.append(
                PlanningDiagnostic(
                    level="warning",
                    code="missing_plan_context",
                    message=(
                        f"Criterion {assessment.criterion_id} has empty plan context: "
                        f"{assessment.source_criterion}"
                    ),
                )
            )

    if submission.risks:
        for risk in submission.risks:
            diagnostics.append(
                PlanningDiagnostic(
                    level="info",
                    code="agent_risk",
                    message=f"Risk: {risk}",
                )
            )

    return tuple(diagnostics)
