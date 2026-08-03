"""Retry policies for refine loops.

A RetryPolicy controls when a refine loop stops trying and what happens
when it does. The policy is queried by the loop after each failed
attempt; the loop body stays unchanged.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class RetryPolicy(Protocol):
    def should_continue(self, attempt: int, failure_kind: str, frame, ctx) -> bool: ...

    def on_exhausted(
        self,
        failure_kind: str,
        error: str,
        all_changed: list[str],
        last_result,
        frame,
        ctx,
    ) -> None: ...

    def describe_limit(self) -> str: ...


class FixedBudgetPolicy:
    """Current behavior: fixed max_attempts, die on exhaustion."""

    def __init__(self, max_attempts: int = 3):
        self.max_attempts = max_attempts

    def should_continue(self, attempt, _failure_kind, _frame, _ctx):
        return attempt < self.max_attempts

    def on_exhausted(
        self, failure_kind, _error, _all_changed, last_result, frame, _ctx
    ):
        from . import pipeline_lib as lib

        exit_code = last_result.returncode if last_result is not None else "unknown"
        what = (
            "Code does not compile"
            if failure_kind == "compile"
            else "test(s) still fail"
        )
        lib.die_with_log(
            "implement-criterion",
            f"{what} after {self.max_attempts} attempt(s) (exit {exit_code}). "
            "See output above. The frame is untouched.",
            criterion=frame.criterion,
        )

    def describe_limit(self):
        return f"{self.max_attempts} attempt(s)"


class EndlessRetryPolicy:
    """Never stops retrying. on_exhausted is never reached."""

    def should_continue(self, _attempt, _failure_kind, _frame, _ctx):
        return True

    def on_exhausted(
        self, _failure_kind, _error, _all_changed, _last_result, frame, _ctx
    ):
        from . import pipeline_lib as lib

        lib.die_with_log(
            "retry-policy",
            "EndlessRetryPolicy.on_exhausted was called unexpectedly. Please report this as a bug.",
            criterion=getattr(frame, "criterion", None),
        )

    def describe_limit(self):
        return "endless"


def _load_retry_config(config_path: Path) -> dict:
    if not config_path.exists():
        return {}
    with config_path.open("rb") as f:
        data = tomllib.load(f)
    retry_cfg = data.get("retry")
    if retry_cfg is None:
        return {}
    if not isinstance(retry_cfg, dict):
        raise ValueError(f"{config_path}: [retry] must be a table")
    unknown = set(retry_cfg) - {"policy", "max_attempts"}
    if unknown:
        raise ValueError(f"{config_path}: [retry] unknown key(s) {sorted(unknown)}")
    return retry_cfg


def resolve_retry_policy(
    config_path: Path, cli_policy: str | None, max_attempts: int
) -> RetryPolicy | None:
    retry_cfg = _load_retry_config(config_path)

    policy_name = cli_policy or retry_cfg.get("policy")
    if policy_name is None:
        return None

    if policy_name == "endless":
        return EndlessRetryPolicy()

    if policy_name != "fixed-budget":
        raise ValueError(
            f"unsupported retry policy {policy_name!r}; expected 'fixed-budget' or 'endless'"
        )

    attempts = max_attempts
    if cli_policy is None and "max_attempts" in retry_cfg:
        attempts_value = retry_cfg["max_attempts"]
        if not isinstance(attempts_value, int) or attempts_value <= 0:
            raise ValueError(
                f"{config_path}: [retry].max_attempts must be a positive integer"
            )
        attempts = attempts_value
    return FixedBudgetPolicy(attempts)
