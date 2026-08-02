"""
Acceptance determination logic for benchmark trials.

Combines individual gate results into a trial-level verdict following the
rules defined in the benchmark specification:

  - ACCEPTED  : all critical gates passed and no non-critical gate counts
                as a mandatory rejection.
  - REJECTED  : at least one critical gate failed (``passed is False``).
  - INDETERMINATE : no critical gate failed, but at least one gate returned
                    ``passed is None``, so automated graders could not reach
                    a confident conclusion.
"""

from __future__ import annotations

from .models import AcceptanceResult, GateResult, Verdict


def determine_verdict(gates: list[GateResult]) -> Verdict:
    """
    Derive the top-level verdict from a list of gate results.

    Rules (applied in order):
    1. If any critical gate has ``passed is False``  → ``rejected``.
    2. If any gate (critical or not) has ``passed is None`` → ``indeterminate``.
    3. Otherwise                                             → ``accepted``.
    """
    for gate in gates:
        if gate.critical and gate.passed is False:
            return "rejected"
    for gate in gates:
        if gate.passed is None:
            return "indeterminate"
    return "accepted"


def build_acceptance_result(
    gates: list[GateResult],
    grader: str,
    confidence: float | None = None,
) -> AcceptanceResult:
    """
    Build an :class:`AcceptanceResult` from a list of gate results.

    Collects reason codes from every failed or indeterminate gate and
    constructs a human-readable explanation.
    """
    verdict = determine_verdict(gates)

    failed_gates = [g for g in gates if g.passed is not True]
    reason_codes: list[str] = []
    for gate in failed_gates:
        code = _reason_code_for(gate)
        if code and code not in reason_codes:
            reason_codes.append(code)

    if verdict == "accepted":
        explanation = "All acceptance gates passed."
    elif verdict == "rejected":
        critical_failures = [g for g in gates if g.critical and g.passed is False]
        lines = [f"  - [{g.gate}] {g.reason}" for g in critical_failures]
        explanation = "Rejected due to critical gate failure(s):\n" + "\n".join(lines)
    else:
        uncertain = [g for g in gates if g.passed is None]
        lines = [f"  - [{g.gate}] {g.reason}" for g in uncertain]
        explanation = "Indeterminate: automated graders could not reach a verdict:\n" + "\n".join(
            lines
        )

    return AcceptanceResult(
        verdict=verdict,
        gates=gates,
        reason_codes=reason_codes,
        explanation=explanation,
        grader=grader,
        confidence=confidence,
    )


def _reason_code_for(gate: GateResult) -> str | None:
    """
    Map a gate name to a canonical reason code, falling back to the gate
    name itself when no specific mapping exists.
    """
    _GATE_TO_CODE: dict[str, str] = {
        "schema_valid": "invalid_schema",
        "required_outcomes_covered": "missing_required_outcome",
        "repository_grounded": "wrong_existing_path",
        "executable": "non_executable_plan",
        "no_critical_false_work": "critical_false_positive",
        "build_passes": "compile_failure",
        "required_tests_pass": "visible_test_failure",
        "hidden_tests_pass": "hidden_test_failure",
        "no_regressions": "regression",
        "repository_invariants_hold": "repository_invariant_violated",
        "test_integrity": "test_weakened",
    }
    return _GATE_TO_CODE.get(gate.gate, gate.gate if gate.passed is not True else None)
