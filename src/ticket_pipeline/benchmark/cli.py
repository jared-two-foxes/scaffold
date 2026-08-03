"""
CLI entry point for the Scaffold strategy benchmark framework.

Commands::

    scaffold benchmark planning  --strategies mechanical,agent \\
                                  --models gpt-5.6 --suite core --trials 5

    scaffold benchmark implementation  --strategies tdd,direct \\
                                        --models gpt-5.6 --suite fixed-red --trials 5

    scaffold benchmark end-to-end  --planning-strategies mechanical,agent \\
                                    --implementation-strategies tdd,direct \\
                                    --models gpt-5.6 --suite core --trials 3

    scaffold benchmark report  --input .scaffold/benchmarks/<run-id>/results.jsonl
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .acceptance import build_acceptance_result
from .fixtures import PlanningFixture, discover_fixtures
from .models import BenchmarkResult, GateResult
from .reporting import load_results, print_planning_detail, print_summary, write_result

# Default locations
_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _SCRIPT_DIR.parents[2]  # scaffold/
_DEFAULT_BENCHMARKS_FIXTURES_DIR = _PROJECT_DIR / "fixtures" / "benchmarks"
_DEFAULT_REPO = Path.home() / "code" / "own" / "VirtualAssistant"
_SCAFFOLD_TEMP_DIR = Path.cwd() / ".scaffold"


def _scaffold_ref() -> str:
    """Return the current Scaffold git commit SHA (best-effort)."""
    try:
        result = subprocess.run(
            ["git", "-C", str(_PROJECT_DIR), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


# ---------------------------------------------------------------------------
# Planning sub-command
# ---------------------------------------------------------------------------


def _run_planning(args: argparse.Namespace) -> None:
    from .planning.runner import run_planning_trial

    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    benchmarks_dir = Path(args.benchmarks_dir)
    suite = args.suite

    fixture_paths = discover_fixtures(benchmarks_dir, "planning", suite)
    if args.cases:
        wanted = {c.strip() for c in args.cases.split(",") if c.strip()}
        fixture_paths = [p for p in fixture_paths if p.name in wanted]

    if not fixture_paths:
        print(
            f"No planning fixtures found under {benchmarks_dir}/planning/{suite}/",
            file=sys.stderr,
        )
        sys.exit(1)

    fixtures: list[PlanningFixture] = []
    for fp in fixture_paths:
        try:
            fixtures.append(PlanningFixture.load(fp))
        except Exception as exc:  # noqa: BLE001
            print(f"WARNING: skipping fixture {fp.name}: {exc}", file=sys.stderr)

    run_id = uuid.uuid4().hex
    output_dir = Path(args.output_dir) / run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    results_path = output_dir / "results.jsonl"
    worktrees_base = _SCAFFOLD_TEMP_DIR / "bench-worktrees"
    scaffold_ref = _scaffold_ref()

    repo = Path(args.repo)

    # Build job list
    jobs: list[tuple[PlanningFixture, str, str, int]] = []
    for fixture in fixtures:
        for strategy in strategies:
            for model in models:
                for rep in range(args.trials):
                    jobs.append((fixture, strategy, model, rep))

    print(
        f"Planning benchmark: {len(jobs)} trial(s) "
        f"({len(fixtures)} fixture(s) × {len(strategies)} strategy × "
        f"{len(models)} model × {args.trials} rep)",
        flush=True,
    )
    print(f"Output: {results_path}", flush=True)

    results: list[BenchmarkResult] = []
    with (
        ThreadPoolExecutor(max_workers=args.max_concurrency) as pool,
        results_path.open("w", encoding="utf-8") as out_f,
    ):
        future_map = {
            pool.submit(
                run_planning_trial,
                fixture,
                strategy,
                model,
                rep,
                repo,
                scaffold_ref,
                worktrees_base,
                args.base_ref or None,
                args.user_input_mode,
            ): (fixture, strategy, model, rep)
            for fixture, strategy, model, rep in jobs
        }
        for future in as_completed(future_map):
            fixture, strategy, model, rep = future_map[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001
                gate = GateResult(
                    gate="runner_error",
                    passed=False,
                    critical=True,
                    reason=f"{type(exc).__name__}: {exc}",
                )
                result = BenchmarkResult(
                    run_id=uuid.uuid4().hex,
                    category="planning",
                    suite=suite,
                    case=fixture.meta.case,
                    strategy=strategy,
                    model=model,
                    repetition=rep,
                    scaffold_ref=scaffold_ref,
                    target_repo_ref=fixture.meta.base_ref,
                    fixture_version=fixture.meta.fixture_version,
                    acceptance=build_acceptance_result([gate], grader="runner"),
                    failure_stage="planning_execution",
                    duration_s=0.0,
                    cost_usd=0.0,
                    input_tokens=0,
                    output_tokens=0,
                    total_tokens=0,
                    attempts=1,
                    tool_calls=0,
                    retries=0,
                    human_interventions=0,
                )
            results.append(result)
            verdict = result.acceptance.verdict.upper()
            print(
                f"  [{verdict}] case={result.case} strategy={result.strategy} "
                f"model={result.model} rep={rep} "
                f"{result.duration_s:.1f}s ${result.cost_usd:.4f}",
                flush=True,
            )
            write_result(result, out_f)

    print_summary(results, title=f"Planning benchmark – suite={suite}")
    if args.verbose:
        print_planning_detail(results)


# ---------------------------------------------------------------------------
# Report sub-command
# ---------------------------------------------------------------------------


def _run_report(args: argparse.Namespace) -> None:
    results = load_results(Path(args.input))
    if not results:
        print("No results found.", file=sys.stderr)
        sys.exit(1)
    title = f"Benchmark report – {args.input}"
    print_summary(results, title=title)
    print_planning_detail(results)


# ---------------------------------------------------------------------------
# Implementation sub-command (Phase 3 stub)
# ---------------------------------------------------------------------------


def _run_implementation(args: argparse.Namespace) -> None:
    print(
        "Implementation benchmark (Phase 3) is not yet implemented.\n"
        "Use 'scaffold benchmark planning' for Phase 2 planning benchmarks.",
        file=sys.stderr,
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# End-to-end sub-command (Phase 6 stub)
# ---------------------------------------------------------------------------


def _run_end_to_end(args: argparse.Namespace) -> None:
    print(
        "End-to-end benchmark (Phase 6) is not yet implemented.\n"
        "Use 'scaffold benchmark planning' for Phase 2 planning benchmarks.",
        file=sys.stderr,
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scaffold benchmark",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    # Common options
    def _add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--models", required=True, help="Comma-separated model IDs")
        p.add_argument("--trials", type=int, default=3)
        p.add_argument("--max-concurrency", type=int, default=4, metavar="N")
        p.add_argument(
            "--repo",
            default=str(_DEFAULT_REPO),
            help=f"Path to target repository (default: {_DEFAULT_REPO})",
        )
        p.add_argument(
            "--base-ref",
            default=None,
            help="Override fixture-pinned commit ref",
        )
        p.add_argument(
            "--output-dir",
            default=str(_SCAFFOLD_TEMP_DIR / "benchmarks"),
            metavar="PATH",
        )
        p.add_argument(
            "--benchmarks-dir",
            default=str(_DEFAULT_BENCHMARKS_FIXTURES_DIR),
            metavar="PATH",
            help="Root of the fixtures/benchmarks directory",
        )
        p.add_argument(
            "--cases",
            default=None,
            help="Comma-separated fixture case names to restrict to",
        )
        p.add_argument("--fail-fast", action="store_true")
        p.add_argument("--verbose", "-v", action="store_true")
        p.add_argument(
            "--user-input-mode",
            default="fail",
            choices=["fail", "infer", "interactive"],
        )

    # planning
    plan_p = sub.add_parser("planning", help="Benchmark planning strategies")
    _add_common(plan_p)
    plan_p.add_argument(
        "--strategies",
        required=True,
        help="Comma-separated planning strategies (e.g. mechanical,agent)",
    )
    plan_p.add_argument("--suite", default="core", help="Fixture suite name")
    plan_p.set_defaults(func=_run_planning)

    # implementation
    impl_p = sub.add_parser("implementation", help="Benchmark implementation strategies")
    _add_common(impl_p)
    impl_p.add_argument(
        "--strategies",
        required=True,
        help="Comma-separated implementation strategies (e.g. tdd,direct)",
    )
    impl_p.add_argument("--suite", default="fixed-red")
    impl_p.set_defaults(func=_run_implementation)

    # end-to-end
    e2e_p = sub.add_parser(
        "end-to-end", help="Benchmark complete planning+implementation pipelines"
    )
    _add_common(e2e_p)
    e2e_p.add_argument("--planning-strategies", required=True)
    e2e_p.add_argument("--implementation-strategies", required=True)
    e2e_p.add_argument("--suite", default="core")
    e2e_p.set_defaults(func=_run_end_to_end)

    # report
    report_p = sub.add_parser("report", help="Generate a report from a results JSONL file")
    report_p.add_argument("--input", required=True, help="Path to results.jsonl")
    report_p.add_argument("--verbose", "-v", action="store_true")
    report_p.set_defaults(func=_run_report)

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
