import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ticket_pipeline.lib import pipeline_lib as lib
from ticket_pipeline.lib.retry import (
    EndlessRetryPolicy,
    FixedBudgetPolicy,
    resolve_retry_policy,
)


class FixedBudgetPolicyTests(unittest.TestCase):
    def test_should_continue_true_below_limit(self):
        policy = FixedBudgetPolicy(3)
        self.assertTrue(policy.should_continue(1, "compile", None, None))
        self.assertTrue(policy.should_continue(2, "compile", None, None))

    def test_should_continue_false_at_limit(self):
        policy = FixedBudgetPolicy(3)
        self.assertFalse(policy.should_continue(3, "compile", None, None))

    def test_describe_limit(self):
        self.assertEqual("3 attempt(s)", FixedBudgetPolicy(3).describe_limit())

    def test_on_exhausted_calls_die_with_log(self):
        policy = FixedBudgetPolicy(3)
        frame = mock.Mock(criterion="criterion")
        with mock.patch.object(lib, "die_with_log") as die:
            policy.on_exhausted("compile", "error", [], None, frame, None)
        die.assert_called_once()


class EndlessRetryPolicyTests(unittest.TestCase):
    def test_should_continue_always_true(self):
        policy = EndlessRetryPolicy()
        for attempt in range(1, 1000):
            self.assertTrue(policy.should_continue(attempt, "compile", None, None))

    def test_describe_limit(self):
        self.assertEqual("endless", EndlessRetryPolicy().describe_limit())


class RetryPolicyConfigTests(unittest.TestCase):
    def test_resolve_endless_from_config(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "pipeline.toml"
            cfg.write_text("[retry]\npolicy = 'endless'\n", encoding="utf-8")
            policy = resolve_retry_policy(cfg, None, 3)
        self.assertIsInstance(policy, EndlessRetryPolicy)

    def test_resolve_fixed_budget_from_cli(self):
        with tempfile.TemporaryDirectory() as d:
            policy = resolve_retry_policy(Path(d) / "missing.toml", "fixed-budget", 5)
        self.assertIsInstance(policy, FixedBudgetPolicy)
        self.assertEqual(5, policy.max_attempts)
