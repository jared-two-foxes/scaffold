"""Tests for the benchmark reporting module."""

import io
import json
import unittest

from ticket_pipeline.benchmark.models import AcceptanceResult, BenchmarkResult
from ticket_pipeline.benchmark.reporting import (
    acceptance_rate_summary,
    print_summary,
    wilson_ci,
    write_result,
)


def _make_result(verdict="accepted", strategy="mechanical", model="gpt-5", case="c1"):
    acceptance = AcceptanceResult(
        verdict=verdict,
        gates=[],
        reason_codes=[],
        explanation="",
        grader="test",
    )
    return BenchmarkResult(
        run_id="abc",
        category="planning",
        suite="core",
        case=case,
        strategy=strategy,
        model=model,
        repetition=0,
        scaffold_ref="abc",
        target_repo_ref="def",
        fixture_version=1,
        acceptance=acceptance,
        failure_stage=None,
        duration_s=10.0,
        cost_usd=0.01,
        input_tokens=50,
        output_tokens=50,
        total_tokens=100,
        attempts=1,
        tool_calls=0,
        retries=0,
        human_interventions=0,
    )


class WriteResultTests(unittest.TestCase):
    def test_write_and_read_back(self):
        result = _make_result("accepted")
        buf = io.StringIO()
        write_result(result, buf)
        buf.seek(0)
        line = buf.read().strip()
        d = json.loads(line)
        self.assertEqual(d["acceptance"]["verdict"], "accepted")

    def test_multiple_results(self):
        buf = io.StringIO()
        for v in ["accepted", "rejected", "indeterminate"]:
            write_result(_make_result(v), buf)
        buf.seek(0)
        lines = [line for line in buf.read().splitlines() if line]
        self.assertEqual(len(lines), 3)


class WilsonCITests(unittest.TestCase):
    def test_zero_trials(self):
        lo, hi = wilson_ci(0, 0)
        self.assertEqual(lo, 0.0)
        self.assertEqual(hi, 1.0)

    def test_all_pass(self):
        lo, hi = wilson_ci(10, 10)
        self.assertGreater(lo, 0.7)
        self.assertLessEqual(hi, 1.0)

    def test_all_fail(self):
        lo, hi = wilson_ci(0, 10)
        self.assertEqual(lo, 0.0)
        self.assertLess(hi, 0.3)

    def test_ci_is_ordered(self):
        lo, hi = wilson_ci(3, 10)
        self.assertLessEqual(lo, hi)


class AcceptanceRateSummaryTests(unittest.TestCase):
    def test_single_strategy(self):
        results = [
            _make_result("accepted", strategy="mechanical"),
            _make_result("rejected", strategy="mechanical"),
            _make_result("accepted", strategy="mechanical"),
        ]
        summary = acceptance_rate_summary(results)
        self.assertIn("mechanical", summary)
        s = summary["mechanical"]
        self.assertEqual(s["trials"], 3)
        self.assertEqual(s["accepted"], 2)
        self.assertAlmostEqual(s["rate"], 2 / 3)

    def test_two_strategies(self):
        results = [
            _make_result("accepted", strategy="mechanical"),
            _make_result("accepted", strategy="agent"),
            _make_result("rejected", strategy="agent"),
        ]
        summary = acceptance_rate_summary(results)
        self.assertEqual(summary["mechanical"]["accepted"], 1)
        self.assertEqual(summary["agent"]["accepted"], 1)

    def test_no_results(self):
        self.assertEqual(acceptance_rate_summary([]), {})


class PrintSummaryTests(unittest.TestCase):
    def test_does_not_crash_with_empty_results(self):
        # Just verify it doesn't raise
        buf = io.StringIO()
        import sys

        old = sys.stdout
        sys.stdout = buf
        try:
            print_summary([])
        finally:
            sys.stdout = old

    def test_does_not_crash_with_results(self):
        results = [
            _make_result("accepted"),
            _make_result("rejected"),
        ]
        buf = io.StringIO()
        import sys

        old = sys.stdout
        sys.stdout = buf
        try:
            print_summary(results)
        finally:
            sys.stdout = old
        output = buf.getvalue()
        self.assertIn("planning", output)


if __name__ == "__main__":
    unittest.main()
