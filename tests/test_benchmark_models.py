"""Tests for the benchmark models module."""

import json
import unittest

from ticket_pipeline.benchmark.models import (
    AcceptanceResult,
    BenchmarkResult,
    GateResult,
)


class GateResultTests(unittest.TestCase):
    def test_round_trip(self):
        gate = GateResult(
            gate="schema_valid",
            passed=True,
            critical=True,
            reason="All good",
            evidence={"count": 3},
        )
        d = gate.to_dict()
        gate2 = GateResult.from_dict(d)
        self.assertEqual(gate2.gate, "schema_valid")
        self.assertTrue(gate2.passed)
        self.assertTrue(gate2.critical)
        self.assertEqual(gate2.reason, "All good")
        self.assertEqual(gate2.evidence, {"count": 3})

    def test_none_passed_round_trip(self):
        gate = GateResult(gate="grounding", passed=None, critical=False, reason="unsure")
        gate2 = GateResult.from_dict(gate.to_dict())
        self.assertIsNone(gate2.passed)


class AcceptanceResultTests(unittest.TestCase):
    def _make_acceptance(self, verdict="accepted"):
        return AcceptanceResult(
            verdict=verdict,
            gates=[GateResult(gate="g", passed=True, critical=True, reason="ok")],
            reason_codes=["code1"],
            explanation="explanation text",
            grader="test_grader",
            confidence=0.9,
        )

    def test_round_trip(self):
        acc = self._make_acceptance("rejected")
        d = acc.to_dict()
        acc2 = AcceptanceResult.from_dict(d)
        self.assertEqual(acc2.verdict, "rejected")
        self.assertEqual(acc2.grader, "test_grader")
        self.assertEqual(acc2.confidence, 0.9)
        self.assertEqual(len(acc2.gates), 1)
        self.assertEqual(acc2.reason_codes, ["code1"])


class BenchmarkResultTests(unittest.TestCase):
    def _make_result(self):
        acceptance = AcceptanceResult(
            verdict="accepted",
            gates=[],
            reason_codes=[],
            explanation="",
            grader="test",
        )
        return BenchmarkResult(
            run_id="abc123",
            category="planning",
            suite="core",
            case="my-case",
            strategy="mechanical",
            model="gpt-5",
            repetition=0,
            scaffold_ref="deadbeef",
            target_repo_ref="feedcafe",
            fixture_version=1,
            acceptance=acceptance,
            failure_stage=None,
            duration_s=12.5,
            cost_usd=0.0042,
            input_tokens=100,
            output_tokens=200,
            total_tokens=300,
            attempts=1,
            tool_calls=5,
            retries=0,
            human_interventions=0,
            changed_files=["src/lib.rs"],
            metrics={"recall": 0.9},
        )

    def test_to_jsonl_and_back(self):
        result = self._make_result()
        line = result.to_jsonl()
        result2 = BenchmarkResult.from_jsonl(line)
        self.assertEqual(result2.run_id, "abc123")
        self.assertEqual(result2.category, "planning")
        self.assertEqual(result2.suite, "core")
        self.assertEqual(result2.case, "my-case")
        self.assertEqual(result2.strategy, "mechanical")
        self.assertEqual(result2.model, "gpt-5")
        self.assertEqual(result2.repetition, 0)
        self.assertAlmostEqual(result2.duration_s, 12.5)
        self.assertAlmostEqual(result2.cost_usd, 0.0042)
        self.assertEqual(result2.input_tokens, 100)
        self.assertEqual(result2.output_tokens, 200)
        self.assertEqual(result2.total_tokens, 300)
        self.assertEqual(result2.changed_files, ["src/lib.rs"])
        self.assertEqual(result2.metrics, {"recall": 0.9})
        self.assertIsNone(result2.failure_stage)
        self.assertEqual(result2.acceptance.verdict, "accepted")

    def test_jsonl_is_valid_json(self):
        result = self._make_result()
        # Must not raise
        parsed = json.loads(result.to_jsonl())
        self.assertIsInstance(parsed, dict)


if __name__ == "__main__":
    unittest.main()
