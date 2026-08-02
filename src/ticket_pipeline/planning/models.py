from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

DiagnosticLevel = Literal["info", "warning", "error"]

VALID_VERIFICATION_MODES = frozenset({"test", "test-refactor", "refactor", "manual"})
VALID_IMPLEMENTATION_STRATEGIES = frozenset({"tdd", "direct", "manual", "refactor"})
_LIST_PREFIX_RE = re.compile(r"^\s*[-*]\s*(?:\[[ xX]\]\s*)?")
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def _criterion_has_meaningful_text(text: str) -> bool:
    stripped = _LIST_PREFIX_RE.sub("", text.strip(), count=1)
    stripped = _HTML_COMMENT_RE.sub("", stripped).strip()
    return bool(stripped)


@dataclass(frozen=True)
class PlanningRequest:
    ticket_id: str
    ticket_content: str
    project_root: Path
    model: str
    step_models: dict[str, str]


@dataclass(frozen=True)
class PlannedCriterion:
    criterion: str
    plan_context: str
    verification: str
    implementation_strategy: str
    existing_test_refs: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.criterion.strip() or not _criterion_has_meaningful_text(self.criterion):
            raise ValueError("criterion must contain meaningful text")
        if not self.verification:
            raise ValueError(
                "verification must be provided explicitly; "
                f"expected one of {sorted(VALID_VERIFICATION_MODES)}"
            )
        if self.verification not in VALID_VERIFICATION_MODES:
            raise ValueError(
                f"unsupported verification mode {self.verification!r}; "
                f"expected one of {sorted(VALID_VERIFICATION_MODES)}"
            )
        if not self.implementation_strategy:
            raise ValueError(
                "implementation_strategy must be provided explicitly; "
                f"expected one of {sorted(VALID_IMPLEMENTATION_STRATEGIES)}"
            )
        if self.implementation_strategy not in VALID_IMPLEMENTATION_STRATEGIES:
            raise ValueError(
                f"unsupported implementation strategy {self.implementation_strategy!r}; "
                f"expected one of {sorted(VALID_IMPLEMENTATION_STRATEGIES)}"
            )
        if any(not ref.strip() for ref in self.existing_test_refs):
            raise ValueError("existing_test_refs must not contain empty values")


@dataclass(frozen=True)
class PlanningDiagnostic:
    level: DiagnosticLevel
    message: str
    code: str | None = None

    def __post_init__(self) -> None:
        if self.level not in {"info", "warning", "error"}:
            raise ValueError(f"unsupported diagnostic level {self.level!r}")
        if not self.message.strip():
            raise ValueError("diagnostic message must be non-empty")


@dataclass(frozen=True)
class PlanningResult:
    criteria: tuple[PlannedCriterion, ...]
    plan_text: str | None = None
    narrowed_plan_text: str | None = None
    diagnostics: tuple[PlanningDiagnostic, ...] = field(default_factory=tuple)
