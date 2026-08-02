"""
agent_models - internal data model for the AgentPlanningStrategy.

These types are used inside the agent planning pipeline and are not
exposed through the public PlanningStrategy interface. The richer model
preserves satisfied criteria, evidence, assumptions, risks, and rationale
without polluting CriterionFrame or PlanningResult with agent-specific fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

CriterionDisposition = Literal[
    "remaining",
    "satisfied",
    "not_applicable",
    "blocked",
]

VALID_DISPOSITIONS: frozenset[str] = frozenset(
    {"remaining", "satisfied", "not_applicable", "blocked"}
)


@dataclass(frozen=True)
class AgentEvidence:
    path: str | None
    observation: str


@dataclass(frozen=True)
class AgentAssumption:
    question: str
    answer: str
    basis: str


@dataclass(frozen=True)
class PlannedChange:
    path: str
    description: str
    symbols: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class AgentCriterionAssessment:
    criterion_id: str
    source_criterion: str
    disposition: CriterionDisposition
    rationale: str
    evidence: tuple[AgentEvidence, ...] = field(default_factory=tuple)
    planned_changes: tuple[PlannedChange, ...] = field(default_factory=tuple)
    verification: str | None = None
    implementation_strategy: str | None = None
    existing_test_refs: tuple[str, ...] = field(default_factory=tuple)
    plan_context: str | None = None
    blocker: str | None = None


@dataclass(frozen=True)
class AgentPlanSubmission:
    ticket_summary: str
    approach_summary: str
    assumptions: tuple[AgentAssumption, ...]
    repository_findings: tuple[AgentEvidence, ...]
    criteria: tuple[AgentCriterionAssessment, ...]
    cross_cutting_changes: tuple[PlannedChange, ...] = field(default_factory=tuple)
    risks: tuple[str, ...] = field(default_factory=tuple)
    validation_notes: tuple[str, ...] = field(default_factory=tuple)
