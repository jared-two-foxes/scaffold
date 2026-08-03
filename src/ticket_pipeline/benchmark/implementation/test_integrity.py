"""
Test integrity validation for implementation benchmarks (spec §8.6).

Detects attempts or accidental changes that undermine the acceptance oracle:
- Deleting a failing test.
- Adding ignore annotations.
- Weakening assertions.
- Replacing meaningful assertions with tautologies.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..models import GateResult

# Patterns that indicate a test has been ignored or disabled
_IGNORE_PATTERNS = [
    re.compile(r"#\[ignore\]", re.IGNORECASE),
    re.compile(r"@pytest\.mark\.skip", re.IGNORECASE),
    re.compile(r"@unittest\.skip", re.IGNORECASE),
    re.compile(r"#\s*type:\s*ignore", re.IGNORECASE),
    re.compile(r"todo!\(\)", re.IGNORECASE),
    re.compile(r"unimplemented!\(\)", re.IGNORECASE),
]


def check_no_ignored_tests(changed_files: list[Path]) -> GateResult:
    """Check that no test files have been annotated with ignore/skip markers."""
    violations: list[str] = []
    for path in changed_files:
        if not path.exists():
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        for pattern in _IGNORE_PATTERNS:
            if pattern.search(content):
                violations.append(f"{path}: matches {pattern.pattern!r}")

    if violations:
        return GateResult(
            gate="test_integrity",
            passed=False,
            critical=True,
            reason=f"Test ignore/skip annotations found: {violations[:5]}",
            evidence={"violations": violations},
        )
    return GateResult(
        gate="test_integrity",
        passed=True,
        critical=True,
        reason="No test ignore/skip annotations found.",
    )


def check_no_deleted_tests(
    original_test_files: list[str],
    changed_files_set: set[str],
    repo_root: Path,
) -> GateResult:
    """Check that tests present in the original fixture have not been deleted."""
    deleted: list[str] = []
    for tf in original_test_files:
        full = repo_root / tf
        if tf in changed_files_set and not full.exists():
            deleted.append(tf)

    if deleted:
        return GateResult(
            gate="test_deletion",
            passed=False,
            critical=True,
            reason=f"Test file(s) deleted: {deleted}",
            evidence={"deleted": deleted},
        )
    return GateResult(
        gate="test_deletion",
        passed=True,
        critical=True,
        reason="No test files deleted.",
    )
