"""
Core data model for the Scaffold strategy benchmark framework.

Every trial resolves to one of three verdicts:
  - ``accepted``  – all mandatory gates passed.
  - ``rejected``  – at least one mandatory gate failed.
  - ``indeterminate`` – automated graders could not determine correctness.

The :class:`BenchmarkResult` is the authoritative per-trial record and is
written to JSONL.  Console and HTML reports are generated from those files.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal

Verdict = Literal["accepted", "rejected", "indeterminate"]

# ---------------------------------------------------------------------------
# Gate-level result
# ---------------------------------------------------------------------------


@dataclass
class GateResult:
    """Outcome of a single acceptance gate."""

    gate: str
    passed: bool | None
    critical: bool
    reason: str
    evidence: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "gate": self.gate,
            "passed": self.passed,
            "critical": self.critical,
            "reason": self.reason,
            "evidence": self.evidence,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "GateResult":
        return cls(
            gate=d["gate"],
            passed=d["passed"],
            critical=d["critical"],
            reason=d["reason"],
            evidence=d.get("evidence", {}),
        )


# ---------------------------------------------------------------------------
# Acceptance result (aggregates gates into a verdict)
# ---------------------------------------------------------------------------


@dataclass
class AcceptanceResult:
    """Aggregated acceptance verdict for a single trial."""

    verdict: Verdict
    gates: list[GateResult]
    reason_codes: list[str]
    explanation: str
    grader: str
    confidence: float | None = None

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "gates": [g.to_dict() for g in self.gates],
            "reason_codes": self.reason_codes,
            "explanation": self.explanation,
            "grader": self.grader,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AcceptanceResult":
        return cls(
            verdict=d["verdict"],
            gates=[GateResult.from_dict(g) for g in d.get("gates", [])],
            reason_codes=d.get("reason_codes", []),
            explanation=d.get("explanation", ""),
            grader=d.get("grader", ""),
            confidence=d.get("confidence"),
        )


# ---------------------------------------------------------------------------
# Full benchmark trial result
# ---------------------------------------------------------------------------


@dataclass
class BenchmarkResult:
    """
    Authoritative record for one benchmark trial.

    Written to JSONL immediately after trial completion so results are
    preserved even if the process is interrupted.
    """

    run_id: str
    category: str  # "planning" | "implementation" | "end_to_end"
    suite: str
    case: str
    strategy: str
    model: str
    repetition: int

    scaffold_ref: str
    target_repo_ref: str
    fixture_version: int

    acceptance: AcceptanceResult
    failure_stage: str | None

    duration_s: float
    cost_usd: float
    input_tokens: int
    output_tokens: int
    total_tokens: int
    attempts: int
    tool_calls: int
    retries: int
    human_interventions: int

    changed_files: list[str] = field(default_factory=list)
    metrics: dict[str, int | float | bool | str] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Serialisation helpers
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "category": self.category,
            "suite": self.suite,
            "case": self.case,
            "strategy": self.strategy,
            "model": self.model,
            "repetition": self.repetition,
            "scaffold_ref": self.scaffold_ref,
            "target_repo_ref": self.target_repo_ref,
            "fixture_version": self.fixture_version,
            "acceptance": self.acceptance.to_dict(),
            "failure_stage": self.failure_stage,
            "duration_s": self.duration_s,
            "cost_usd": self.cost_usd,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "attempts": self.attempts,
            "tool_calls": self.tool_calls,
            "retries": self.retries,
            "human_interventions": self.human_interventions,
            "changed_files": self.changed_files,
            "metrics": self.metrics,
        }

    def to_jsonl(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, d: dict) -> "BenchmarkResult":
        return cls(
            run_id=d["run_id"],
            category=d["category"],
            suite=d["suite"],
            case=d["case"],
            strategy=d["strategy"],
            model=d["model"],
            repetition=d["repetition"],
            scaffold_ref=d["scaffold_ref"],
            target_repo_ref=d["target_repo_ref"],
            fixture_version=d["fixture_version"],
            acceptance=AcceptanceResult.from_dict(d["acceptance"]),
            failure_stage=d.get("failure_stage"),
            duration_s=d["duration_s"],
            cost_usd=d["cost_usd"],
            input_tokens=d.get("input_tokens", 0),
            output_tokens=d.get("output_tokens", 0),
            total_tokens=d.get("total_tokens", 0),
            attempts=d.get("attempts", 1),
            tool_calls=d.get("tool_calls", 0),
            retries=d.get("retries", 0),
            human_interventions=d.get("human_interventions", 0),
            changed_files=d.get("changed_files", []),
            metrics=d.get("metrics", {}),
        )

    @classmethod
    def from_jsonl(cls, line: str) -> "BenchmarkResult":
        return cls.from_dict(json.loads(line))
