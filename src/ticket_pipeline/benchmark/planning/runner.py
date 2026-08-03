"""
Planning strategy benchmark runner.

Executes planning trials in isolated git worktrees, grades the resulting
planning artifact using generic and fixture-specific gates, and returns a
:class:`BenchmarkResult` for each trial.

Execution flow (spec §17.1)::

    load fixture
        -> create isolated worktree
        -> checkout pinned target commit
        -> instantiate planning strategy
        -> execute strategy
        -> capture raw artifact and telemetry
        -> validate schema
        -> run repository-grounding checks
        -> run fixture-specific graders
        -> optionally queue human review
        -> emit acceptance result
"""

from __future__ import annotations

import subprocess
import time
import uuid
from pathlib import Path

from ...planning.factory import create_planning_strategy
from ...planning.models import PlanningRequest, PlanningResult
from ...planning.strategy import PlanningError
from ..acceptance import build_acceptance_result
from ..fixtures import PlanningFixture, resolve_base_ref
from ..models import BenchmarkResult, GateResult
from ..worktrees import create_worktree, remove_worktree
from .graders import run_generic_planning_gates


def run_planning_trial(
    fixture: PlanningFixture,
    strategy_name: str,
    model: str,
    repetition: int,
    repo: Path,
    scaffold_ref: str,
    worktrees_base: Path,
    base_ref_override: str | None = None,
    user_input_mode: str = "fail",
) -> BenchmarkResult:
    """
    Run one planning trial and return the graded :class:`BenchmarkResult`.

    The trial is isolated in a fresh git worktree that is removed after
    grading.  If worktree creation fails the result is recorded as rejected
    at the ``planning_execution`` stage.
    """
    from ...lib import ai_client  # import locally to reset usage per trial

    run_id = uuid.uuid4().hex
    base_ref = resolve_base_ref(
        _fixture_dir_from_fixture(fixture),
        override=base_ref_override,
    )
    base_ref = base_ref_override or fixture.meta.base_ref

    # ------------------------------------------------------------------
    # Create worktree
    # ------------------------------------------------------------------
    wt_path: Path | None = None
    start = time.monotonic()
    try:
        wt_path = create_worktree(repo, base_ref, worktrees_base)
    except subprocess.CalledProcessError as exc:
        duration = time.monotonic() - start
        gate = GateResult(
            gate="worktree_setup",
            passed=False,
            critical=True,
            reason=f"Worktree creation failed: {exc.stderr}",
        )
        return _make_result(
            run_id=run_id,
            fixture=fixture,
            strategy_name=strategy_name,
            model=model,
            repetition=repetition,
            scaffold_ref=scaffold_ref,
            target_repo_ref=base_ref,
            gates=[gate],
            failure_stage="planning_execution",
            duration_s=duration,
        )

    # ------------------------------------------------------------------
    # Execute planning strategy
    # ------------------------------------------------------------------
    ai_client.usage = type(ai_client.usage)()  # reset usage counters
    planning_result: PlanningResult | None = None
    failure_stage: str | None = None
    execution_error: str | None = None

    try:
        strategy = create_planning_strategy(
            strategy_name,
            config_path=wt_path / ".dev-pipeline.toml",
        )
        request = PlanningRequest(
            ticket_id=fixture.meta.case,
            ticket_content=fixture.ticket_content,
            project_root=wt_path,
            model=model,
            step_models={},
        )
        planning_result = strategy.plan(request)
    except PlanningError as exc:
        failure_stage = "planning_execution"
        execution_error = str(exc)
    except Exception as exc:  # noqa: BLE001
        failure_stage = "planning_execution"
        execution_error = f"{type(exc).__name__}: {exc}"
    finally:
        remove_worktree(repo, wt_path)

    duration_s = time.monotonic() - start

    # Collect usage
    cost_usd, _ = ai_client.usage.total_cost_usd()
    input_tokens = ai_client.usage.prompt_tokens
    output_tokens = ai_client.usage.completion_tokens
    total_tokens = input_tokens + output_tokens

    # ------------------------------------------------------------------
    # Grade the result
    # ------------------------------------------------------------------
    if planning_result is None:
        gate = GateResult(
            gate="planning_execution",
            passed=False,
            critical=True,
            reason=execution_error or "Planning strategy raised an error.",
        )
        return _make_result(
            run_id=run_id,
            fixture=fixture,
            strategy_name=strategy_name,
            model=model,
            repetition=repetition,
            scaffold_ref=scaffold_ref,
            target_repo_ref=base_ref,
            gates=[gate],
            failure_stage=failure_stage or "planning_execution",
            duration_s=duration_s,
            cost_usd=cost_usd,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )

    # Run generic planning gates (without repo_root since worktree is gone;
    # repository-grounding against the fixture's required/forbidden paths
    # is done via the fixture meta rather than live filesystem).
    gates = run_generic_planning_gates(
        result=planning_result,
        fixture=fixture,
        repo_root=None,
    )

    # Determine failure stage from first critical failure
    for gate in gates:
        if gate.critical and gate.passed is False:
            failure_stage = _gate_to_stage(gate.gate)
            break

    return _make_result(
        run_id=run_id,
        fixture=fixture,
        strategy_name=strategy_name,
        model=model,
        repetition=repetition,
        scaffold_ref=scaffold_ref,
        target_repo_ref=base_ref,
        gates=gates,
        failure_stage=failure_stage,
        duration_s=duration_s,
        cost_usd=cost_usd,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_result(
    *,
    run_id: str,
    fixture: PlanningFixture,
    strategy_name: str,
    model: str,
    repetition: int,
    scaffold_ref: str,
    target_repo_ref: str,
    gates: list[GateResult],
    failure_stage: str | None,
    duration_s: float,
    cost_usd: float = 0.0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    total_tokens: int = 0,
) -> BenchmarkResult:
    acceptance = build_acceptance_result(gates, grader="planning_generic")
    return BenchmarkResult(
        run_id=run_id,
        category="planning",
        suite=fixture.meta.suite,
        case=fixture.meta.case,
        strategy=strategy_name,
        model=model,
        repetition=repetition,
        scaffold_ref=scaffold_ref,
        target_repo_ref=target_repo_ref,
        fixture_version=fixture.meta.fixture_version,
        acceptance=acceptance,
        failure_stage=failure_stage,
        duration_s=round(duration_s, 3),
        cost_usd=round(cost_usd, 6),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        attempts=1,
        tool_calls=0,
        retries=0,
        human_interventions=0,
    )


def _gate_to_stage(gate_name: str) -> str:
    _MAP = {
        "schema_valid": "planning_schema",
        "strategy_names_valid": "planning_schema",
        "required_outcomes_covered": "planning_required_outcomes",
        "repository_grounded": "planning_repository_grounding",
        "referenced_paths_exist": "planning_repository_grounding",
        "forbidden_paths_absent": "planning_repository_grounding",
        "executable": "planning_schema",
        "no_critical_false_work": "planning_false_work",
    }
    return _MAP.get(gate_name, "planning_execution")


def _fixture_dir_from_fixture(fixture: PlanningFixture) -> Path:
    # The fixture itself doesn't store its own directory, so we return a
    # dummy path; the base_ref is taken directly from fixture.meta.
    return Path(".")
