"""
Hidden acceptance test runner for implementation benchmarks (spec §8.3).

Hidden tests verify the ticket outcome independently of tests created or
observed by the implementation strategy.  They protect against:

- Visible-test overfitting.
- Weak generated tests.
- Assertions that do not exercise required behavior.
- Implementations that satisfy only a narrow example.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ..models import GateResult


def run_hidden_tests(hidden_test_cmd: str, cwd: str | Path) -> GateResult:
    """
    Execute the fixture-defined hidden acceptance test command.

    Returns INDETERMINATE (not rejected) when no command is configured,
    since absence of hidden tests means we simply cannot verify.
    """
    if not hidden_test_cmd:
        return GateResult(
            gate="hidden_tests_pass",
            passed=None,
            critical=False,
            reason="No hidden test command configured for this fixture.",
        )

    result = subprocess.run(
        hidden_test_cmd,
        shell=True,
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return GateResult(
            gate="hidden_tests_pass",
            passed=False,
            critical=True,
            reason=f"Hidden tests failed (exit {result.returncode}).",
            evidence={
                "stderr": result.stderr[-2000:],
                "stdout": result.stdout[-2000:],
            },
        )
    return GateResult(
        gate="hidden_tests_pass",
        passed=True,
        critical=True,
        reason="Hidden tests passed.",
    )
