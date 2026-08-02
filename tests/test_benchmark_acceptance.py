"""Tests for the benchmark acceptance module."""
import unittest

from ticket_pipeline.benchmark.acceptance import build_acceptance_result, determine_verdict
from ticket_pipeline.benchmark.models import GateResult


class DetermineVerdictTests(unittest.TestCase):
    def test_all_pass_gives_accepted(self):
        gates = [
            GateResult(gate="a", passed=True, critical=True, reason="ok"),
            GateResult(gate="b", passed=True, critical=False, reason="ok"),
        ]
        self.assertEqual(determine_verdict(gates), "accepted")

    def test_critical_fail_gives_rejected(self):
        gates = [
            GateResult(gate="a", passed=True, critical=True, reason="ok"),
            GateResult(gate="b", passed=False, critical=True, reason="bad"),
        ]
        self.assertEqual(determine_verdict(gates), "rejected")

    def test_non_critical_fail_does_not_reject(self):
        gates = [
            GateResult(gate="a", passed=True, critical=True, reason="ok"),
            GateResult(gate="b", passed=False, critical=False, reason="minor"),
        ]
        # Should be accepted unless there is also a None gate
        self.assertEqual(determine_verdict(gates), "accepted")

    def test_none_gate_gives_indeterminate_when_no_critical_failure(self):
        gates = [
            GateResult(gate="a", passed=True, critical=True, reason="ok"),
            GateResult(gate="b", passed=None, critical=False, reason="unsure"),
        ]
        self.assertEqual(determine_verdict(gates), "indeterminate")

    def test_critical_failure_takes_priority_over_none(self):
        """Rejected should be returned even when there is also an indeterminate gate."""
        gates = [
            GateResult(gate="a", passed=False, critical=True, reason="bad"),
            GateResult(gate="b", passed=None, critical=False, reason="unsure"),
        ]
        self.assertEqual(determine_verdict(gates), "rejected")

    def test_empty_gates_gives_accepted(self):
        self.assertEqual(determine_verdict([]), "accepted")


class BuildAcceptanceResultTests(unittest.TestCase):
    def test_accepted_result_explanation(self):
        gates = [GateResult(gate="a", passed=True, critical=True, reason="ok")]
        result = build_acceptance_result(gates, grader="test")
        self.assertEqual(result.verdict, "accepted")
        self.assertIn("passed", result.explanation.lower())
        self.assertEqual(result.grader, "test")

    def test_rejected_result_explanation_includes_gate_name(self):
        gates = [
            GateResult(gate="schema_valid", passed=False, critical=True, reason="missing field"),
        ]
        result = build_acceptance_result(gates, grader="test")
        self.assertEqual(result.verdict, "rejected")
        self.assertIn("schema_valid", result.explanation)
        self.assertIn("missing field", result.explanation)

    def test_rejected_adds_reason_code(self):
        gates = [
            GateResult(gate="schema_valid", passed=False, critical=True, reason="bad"),
        ]
        result = build_acceptance_result(gates, grader="test")
        self.assertIn("invalid_schema", result.reason_codes)

    def test_indeterminate_explanation_mentions_gate(self):
        gates = [
            GateResult(gate="repository_grounded", passed=None, critical=False, reason="unclear"),
        ]
        result = build_acceptance_result(gates, grader="test")
        self.assertEqual(result.verdict, "indeterminate")
        self.assertIn("repository_grounded", result.explanation)

    def test_confidence_is_preserved(self):
        gates = [GateResult(gate="a", passed=True, critical=True, reason="ok")]
        result = build_acceptance_result(gates, grader="test", confidence=0.85)
        self.assertAlmostEqual(result.confidence, 0.85)

    def test_no_duplicate_reason_codes(self):
        gates = [
            GateResult(gate="schema_valid", passed=False, critical=True, reason="bad1"),
            GateResult(gate="schema_valid", passed=False, critical=True, reason="bad2"),
        ]
        result = build_acceptance_result(gates, grader="test")
        self.assertEqual(result.reason_codes.count("invalid_schema"), 1)


if __name__ == "__main__":
    unittest.main()
