"""
agent_prompt - builds the initial planning prompt for AgentPlanningStrategy.

Assembles the system instructions, ticket content, repository orientation,
toolchain information, and acceptance criteria contract into the initial
user message for the planning agent.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..lib import repo_context
from ..lib.pipeline_lib import PROMPTS_DIR
from ..lib.repo_context import gather_repo_context, render_repo_context_block
from .models import PlanningRequest

_AGENT_PLAN_PROMPT_FILE = PROMPTS_DIR / "agent-plan.prompt.md"

# Strip YAML front-matter from the prompt file if present.
_FRONT_MATTER_RE = re.compile(r"^---\n.*?\n---\n", re.DOTALL)


def _load_agent_plan_prompt() -> str:
    text = _AGENT_PLAN_PROMPT_FILE.read_text(encoding="utf-8")
    return _FRONT_MATTER_RE.sub("", text, count=1).strip()


def extract_acceptance_criteria(ticket_content: str) -> list[str]:
    """
    Extract explicit acceptance criteria from the ticket content and
    assign stable IDs (AC-1, AC-2, …).

    Looks for a section headed 'Acceptance Criteria', 'Definition of Done',
    or 'AC', then returns each bullet/checklist item as a separate criterion.
    Returns an empty list if no such section is found.
    """
    # Find an acceptance criteria section heading
    pattern = re.compile(
        r"(?:^|\n)#+\s*(?:acceptance criteria|definition of done|ac)\s*\n(.*?)(?=\n#+\s|\Z)",
        re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(ticket_content)
    if not match:
        # Also try un-headed checklist at top level
        return []

    section = match.group(1)
    # Extract checklist/bullet items
    items = re.findall(r"^\s*[-*]\s*(?:\[[ xX]\]\s*)?(.+)", section, re.MULTILINE)
    return [item.strip() for item in items if item.strip()]


def assign_criterion_ids(criteria: list[str]) -> list[tuple[str, str]]:
    """
    Return [(criterion_id, criterion_text), ...] with stable AC-N IDs.
    """
    return [(f"AC-{i + 1}", text) for i, text in enumerate(criteria)]


def build_agent_plan_prompt(
    request: PlanningRequest,
    criterion_ids: list[tuple[str, str]],
) -> str:
    """
    Build the initial planning prompt for the agent.

    Includes:
    - Agent instructions from agent-plan.prompt.md
    - Repository orientation (tree + toolchain)
    - Ticket content
    - Criterion contract (IDs + text)
    - Cheaply prefetched referenced files (if any)
    """
    instructions = _load_agent_plan_prompt()

    # Repository orientation
    ctx = gather_repo_context(request.project_root)
    orientation = render_repo_context_block(ctx)

    # Criterion contract block
    if criterion_ids:
        crit_lines = ["## Acceptance criteria to assess", ""]
        for cid, text in criterion_ids:
            crit_lines.append(f"- **{cid}**: {text}")
        criterion_block = "\n".join(crit_lines)
    else:
        criterion_block = (
            "## Acceptance criteria to assess\n\n"
            "No explicit acceptance criteria found. Derive criteria from the "
            "ticket description. Mark each as `derived: true` in your rationale."
        )

    # Model key hint
    model_info = f"Model: {request.model}"

    prompt = "\n\n---\n\n".join(
        [
            instructions,
            f"## Repository orientation\n\n{orientation}",
            f"## Ticket: {request.ticket_id}\n\n{request.ticket_content}",
            criterion_block,
            model_info,
        ]
    )
    return prompt
