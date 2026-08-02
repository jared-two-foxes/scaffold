"""
Implementation strategy benchmark runner (Phase 3 placeholder).

Full implementation gates (spec §8):

    implementation_accepted =
        build_passes
        AND required_tests_pass
        AND hidden_tests_pass
        AND no_regressions
        AND repository_invariants_hold
"""

from __future__ import annotations

from ..models import GateResult


def grade_build_passes(build_cmd: str, cwd: str) -> GateResult:
    """Run the fixture build command and return a gate result."""
    import subprocess

    if not build_cmd:
        return GateResult(
            gate="build_passes",
            passed=None,
            critical=True,
            reason="No build command configured for this fixture.",
        )

    result = subprocess.run(
        build_cmd,
        shell=True,
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return GateResult(
            gate="build_passes",
            passed=False,
            critical=True,
            reason=f"Build failed (exit {result.returncode}).",
            evidence={"stderr": result.stderr[-2000:], "stdout": result.stdout[-2000:]},
        )
    return GateResult(
        gate="build_passes",
        passed=True,
        critical=True,
        reason="Build succeeded.",
    )


def grade_tests_pass(test_cmd: str, cwd: str, gate_name: str) -> GateResult:
    """Run a test command and return a gate result."""
    import subprocess

    if not test_cmd:
        return GateResult(
            gate=gate_name,
            passed=None,
            critical=False,
            reason=f"No {gate_name} command configured for this fixture.",
        )

    result = subprocess.run(
        test_cmd,
        shell=True,
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return GateResult(
            gate=gate_name,
            passed=False,
            critical=True,
            reason=f"{gate_name} failed (exit {result.returncode}).",
            evidence={"stderr": result.stderr[-2000:], "stdout": result.stdout[-2000:]},
        )
    return GateResult(
        gate=gate_name,
        passed=True,
        critical=True,
        reason=f"{gate_name} passed.",
    )
