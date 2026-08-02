"""
Fixture loading utilities for the benchmark framework.

Fixtures live under ``fixtures/benchmarks/`` relative to the project root and
are divided by category and suite::

    fixtures/benchmarks/
      planning/
        core/
          <case>/
            fixture.json
            ticket.md
            expected.json
            reviewer-notes.md   (optional, never exposed to the strategy)
      implementation/
        fixed-red/
          <case>/
        end-to-end-tdd/
          <case>/
        refactor/
          <case>/
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Shared fixture metadata
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FixtureMeta:
    """Metadata parsed from ``fixture.json``."""

    fixture_version: int
    category: str
    suite: str
    case: str
    target_repo: str
    base_ref: str
    case_type: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "FixtureMeta":
        return cls(
            fixture_version=d.get("fixture_version", 1),
            category=d["category"],
            suite=d["suite"],
            case=d["case"],
            target_repo=d.get("target_repo", ""),
            base_ref=d.get("base_ref", "HEAD"),
            case_type=d.get("case_type", ""),
        )

    @classmethod
    def load(cls, fixture_dir: Path) -> "FixtureMeta":
        meta_path = fixture_dir / "fixture.json"
        if not meta_path.is_file():
            raise FileNotFoundError(f"Missing fixture.json in {fixture_dir}")
        return cls.from_dict(json.loads(meta_path.read_text(encoding="utf-8")))


# ---------------------------------------------------------------------------
# Planning fixture
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RequiredOutcome:
    id: str
    description: str
    critical: bool = True


@dataclass(frozen=True)
class PlanningFixture:
    meta: FixtureMeta
    ticket_content: str
    required_outcomes: tuple[RequiredOutcome, ...]
    required_existing_paths: tuple[str, ...]
    forbidden_paths: tuple[str, ...]
    already_satisfied_outcomes: tuple[str, ...]
    expected_strategy_by_outcome: dict[str, str]
    critical_false_work_patterns: tuple[str, ...]

    @classmethod
    def load(cls, fixture_dir: Path) -> "PlanningFixture":
        meta = FixtureMeta.load(fixture_dir)

        ticket_path = fixture_dir / "ticket.md"
        if not ticket_path.is_file():
            raise FileNotFoundError(f"Missing ticket.md in {fixture_dir}")
        ticket_content = ticket_path.read_text(encoding="utf-8")

        expected_path = fixture_dir / "expected.json"
        if not expected_path.is_file():
            raise FileNotFoundError(f"Missing expected.json in {fixture_dir}")
        expected = json.loads(expected_path.read_text(encoding="utf-8"))

        required_outcomes = tuple(
            RequiredOutcome(
                id=o["id"],
                description=o["description"],
                critical=o.get("critical", True),
            )
            for o in expected.get("required_outcomes", [])
        )

        return cls(
            meta=meta,
            ticket_content=ticket_content,
            required_outcomes=required_outcomes,
            required_existing_paths=tuple(expected.get("required_existing_paths", [])),
            forbidden_paths=tuple(expected.get("forbidden_paths", [])),
            already_satisfied_outcomes=tuple(
                expected.get("already_satisfied_outcomes", [])
            ),
            expected_strategy_by_outcome=expected.get("expected_strategy_by_outcome", {}),
            critical_false_work_patterns=tuple(
                expected.get("critical_false_work_patterns", [])
            ),
        )


# ---------------------------------------------------------------------------
# Implementation fixture
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GradingConfig:
    build_cmd: str = ""
    required_test_cmd: str = ""
    hidden_test_cmd: str = ""
    regression_cmd: str = ""
    allowed_changed_paths: tuple[str, ...] = field(default_factory=tuple)
    forbidden_changed_paths: tuple[str, ...] = field(default_factory=tuple)
    forbid_test_deletion: bool = True
    forbid_ignored_tests: bool = True

    @classmethod
    def from_dict(cls, d: dict) -> "GradingConfig":
        return cls(
            build_cmd=d.get("build_cmd", ""),
            required_test_cmd=d.get("required_test_cmd", ""),
            hidden_test_cmd=d.get("hidden_test_cmd", ""),
            regression_cmd=d.get("regression_cmd", ""),
            allowed_changed_paths=tuple(d.get("allowed_changed_paths", [])),
            forbidden_changed_paths=tuple(d.get("forbidden_changed_paths", [])),
            forbid_test_deletion=d.get("forbid_test_deletion", True),
            forbid_ignored_tests=d.get("forbid_ignored_tests", True),
        )


@dataclass(frozen=True)
class CriterionFrame:
    criterion: str
    plan_context: str
    verification: str
    origin: str
    existing_test_refs: tuple[str, ...]
    starting_status: str


@dataclass(frozen=True)
class ImplementationFixture:
    meta: FixtureMeta
    ticket_content: str
    criterion_frame: CriterionFrame
    grading: GradingConfig

    @classmethod
    def load(cls, fixture_dir: Path) -> "ImplementationFixture":
        meta = FixtureMeta.load(fixture_dir)

        ticket_path = fixture_dir / "ticket.md"
        ticket_content = ticket_path.read_text(encoding="utf-8") if ticket_path.is_file() else ""

        cf_path = fixture_dir / "criterion-frame.json"
        if not cf_path.is_file():
            raise FileNotFoundError(f"Missing criterion-frame.json in {fixture_dir}")
        cf_data = json.loads(cf_path.read_text(encoding="utf-8"))
        criterion_frame = CriterionFrame(
            criterion=cf_data["criterion"],
            plan_context=cf_data.get("plan_context", ""),
            verification=cf_data.get("verification", "test"),
            origin=cf_data.get("origin", "fixture"),
            existing_test_refs=tuple(cf_data.get("existing_test_refs", [])),
            starting_status=cf_data.get("starting_status", "pending"),
        )

        grading_path = fixture_dir / "grading.toml"
        grading: GradingConfig
        if grading_path.is_file():
            import tomllib

            with grading_path.open("rb") as f:
                grading = GradingConfig.from_dict(tomllib.load(f))
        else:
            grading = GradingConfig()

        return cls(
            meta=meta,
            ticket_content=ticket_content,
            criterion_frame=criterion_frame,
            grading=grading,
        )


# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------


def discover_fixtures(benchmarks_dir: Path, category: str, suite: str) -> list[Path]:
    """
    Return sorted paths to all fixture directories for a given category and suite.

    Each returned path is a directory containing at least ``fixture.json``.
    """
    suite_dir = benchmarks_dir / category / suite
    if not suite_dir.is_dir():
        return []
    return sorted(
        p for p in suite_dir.iterdir() if p.is_dir() and (p / "fixture.json").is_file()
    )


def resolve_base_ref(fixture_dir: Path, override: str | None = None) -> str:
    """
    Determine the target-repo ref for a fixture.

    Precedence: explicit override > fixture.json ``base_ref`` > ``"HEAD"``
    (with a warning).
    """
    if override:
        return override
    meta_path = fixture_dir / "fixture.json"
    if not meta_path.is_file():
        _warn_no_pin(fixture_dir)
        return "HEAD"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    ref = meta.get("base_ref")
    if not ref:
        _warn_no_pin(fixture_dir)
        return "HEAD"
    return ref


def _warn_no_pin(fixture_dir: Path) -> None:
    print(
        f"-- warning: no base_ref pin found in {fixture_dir}/fixture.json – "
        "using 'HEAD', which moves as the target repo changes. "
        "Results from this run may not be reproducible. "
        "Add a base_ref to fixture.json to pin the commit.",
        flush=True,
    )
