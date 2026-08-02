"""
Reporting utilities for the benchmark framework.

Supports:
- Writing :class:`BenchmarkResult` records to a JSONL file.
- Reading JSONL files back into result objects.
- Generating console summary tables.

Primary metrics (accepted-outcome rate, cost/accepted, time/accepted) are
always presented first, with efficiency metrics as secondary context.
"""

from __future__ import annotations

import json
import math
import statistics
from pathlib import Path
from typing import IO

from .models import BenchmarkResult, Verdict


# ---------------------------------------------------------------------------
# JSONL I/O
# ---------------------------------------------------------------------------


def write_result(result: BenchmarkResult, out: IO[str]) -> None:
    """Append one result as a JSONL line and flush immediately."""
    out.write(result.to_jsonl() + "\n")
    out.flush()


def load_results(jsonl_path: Path) -> list[BenchmarkResult]:
    """Load all results from a JSONL file."""
    results: list[BenchmarkResult] = []
    with jsonl_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                results.append(BenchmarkResult.from_jsonl(line))
    return results


# ---------------------------------------------------------------------------
# Console report
# ---------------------------------------------------------------------------


def print_summary(results: list[BenchmarkResult], title: str = "Benchmark summary") -> None:
    """
    Print a strategy-level summary table to stdout.

    Primary columns are acceptance counts, secondary columns are efficiency.
    """
    if not results:
        print(f"\n{title}\n(no results)\n")
        return

    # Group by (category, strategy, model)
    groups: dict[tuple[str, str, str], list[BenchmarkResult]] = {}
    for r in results:
        key = (r.category, r.strategy, r.model)
        groups.setdefault(key, []).append(r)

    print(f"\n{title}\n{'=' * len(title)}")
    header = (
        f"{'category':<18} {'strategy':<12} {'model':<20} "
        f"{'accepted':<10} {'rejected':<10} {'indet':<7} "
        f"{'cost/acc':<10} {'time/acc':<10}"
    )
    print(header)
    print("-" * len(header))

    for (category, strategy, model), group in sorted(groups.items()):
        accepted = [r for r in group if r.acceptance.verdict == "accepted"]
        rejected = [r for r in group if r.acceptance.verdict == "rejected"]
        indet = [r for r in group if r.acceptance.verdict == "indeterminate"]
        n = len(group)

        cost_per_acc = (
            sum(r.cost_usd for r in group) / len(accepted) if accepted else float("inf")
        )
        time_per_acc = (
            sum(r.duration_s for r in group) / len(accepted) if accepted else float("inf")
        )

        def _fmt_rate(count: int) -> str:
            return f"{count}/{n}"

        def _fmt_eff(v: float) -> str:
            if math.isinf(v):
                return "–"
            if v < 1:
                return f"${v:.3f}"
            return f"{v:.1f}s" if v > 1 else f"${v:.4f}"

        cost_str = f"${cost_per_acc:.3f}" if not math.isinf(cost_per_acc) else "–"
        time_str = f"{time_per_acc:.1f}s" if not math.isinf(time_per_acc) else "–"

        print(
            f"{category:<18} {strategy:<12} {model:<20} "
            f"{_fmt_rate(len(accepted)):<10} {_fmt_rate(len(rejected)):<10} "
            f"{_fmt_rate(len(indet)):<7} "
            f"{cost_str:<10} {time_str:<10}"
        )
    print()


def print_planning_detail(results: list[BenchmarkResult]) -> None:
    """
    Print per-case planning quality metrics for accepted/rejected planning
    trials.
    """
    planning = [r for r in results if r.category == "planning"]
    if not planning:
        return

    print("\nPlanning quality metrics")
    print("=" * 24)
    for r in planning:
        verdict = r.acceptance.verdict.upper()
        print(
            f"  [{verdict}] case={r.case} strategy={r.strategy} "
            f"model={r.model} rep={r.repetition}"
        )
        for gate in r.acceptance.gates:
            status = "PASS" if gate.passed is True else ("SKIP" if gate.passed is None else "FAIL")
            crit_marker = "!" if gate.critical else " "
            print(f"    {crit_marker} [{status}] {gate.gate}: {gate.reason}")
        if r.failure_stage:
            print(f"    failure_stage={r.failure_stage}")
        print()


# ---------------------------------------------------------------------------
# Binomial confidence interval (Wilson score)
# ---------------------------------------------------------------------------


def wilson_ci(successes: int, trials: int, z: float = 1.96) -> tuple[float, float]:
    """
    Return the Wilson score 95% confidence interval for an acceptance rate.

    Returns ``(lower, upper)`` as fractions in [0, 1].  Returns ``(0, 1)``
    when *trials* is 0.
    """
    if trials == 0:
        return 0.0, 1.0
    p = successes / trials
    denom = 1 + z * z / trials
    centre = (p + z * z / (2 * trials)) / denom
    margin = (z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials))) / denom
    return max(0.0, centre - margin), min(1.0, centre + margin)


def acceptance_rate_summary(
    results: list[BenchmarkResult],
) -> dict[str, dict]:
    """
    Return a dict mapping ``strategy`` to acceptance-rate statistics.

    Each value is::

        {
            "trials": int,
            "accepted": int,
            "rate": float,
            "ci_low": float,
            "ci_high": float,
            "cost_per_accepted": float | None,
            "time_per_accepted": float | None,
        }
    """
    groups: dict[str, list[BenchmarkResult]] = {}
    for r in results:
        groups.setdefault(r.strategy, []).append(r)

    summary: dict[str, dict] = {}
    for strategy, group in sorted(groups.items()):
        n = len(group)
        acc = sum(1 for r in group if r.acceptance.verdict == "accepted")
        ci_lo, ci_hi = wilson_ci(acc, n)
        total_cost = sum(r.cost_usd for r in group)
        total_time = sum(r.duration_s for r in group)
        summary[strategy] = {
            "trials": n,
            "accepted": acc,
            "rate": acc / n if n else 0.0,
            "ci_low": ci_lo,
            "ci_high": ci_hi,
            "cost_per_accepted": total_cost / acc if acc else None,
            "time_per_accepted": total_time / acc if acc else None,
        }
    return summary
