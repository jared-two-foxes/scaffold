"""Explicit administrative reset of Scaffold's persisted feature/task state.

This is a human-operated CLI, not a task lifecycle operation.
It never changes Git, application files, evidence stores or historic stack files.
"""

from __future__ import annotations

import argparse
import sys

from ticket_pipeline.lib import state_store
from ticket_pipeline.lib.task_reset import ResetPlan, apply_reset, plan_reset


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Close Scaffold features (ledgers are kept; not Git or source files).",
        epilog=(
            "Examples: scaffold clean --feature SA-585 --dry-run; "
            "scaffold clean --feature SA-585; scaffold clean --all --yes"
        ),
    )
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--feature", metavar="ID", help="Close only this feature")
    scope.add_argument("--all", action="store_true", help="Close every open feature")
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview only; never close features"
    )
    parser.add_argument(
        "--yes", action="store_true", help="Approve the displayed reset without a prompt"
    )
    return parser


def _show_plan(plan: ResetPlan) -> None:
    target = f"feature {plan.feature_id}" if plan.scope == "feature" else "all features"
    print(f"Scaffold state reset: {target}")
    print(f"Ledgers: {state_store.SCAFFOLD_DIR / 'features'}")
    print(f"Affected features: {len(plan.affected_features)}")
    print(f"Affected tasks/issues: {plan.total_tasks}")
    for feature in plan.affected_features:
        print(f"  {feature.id}: {feature.task_count} tasks/issues")
    if not plan.will_change:
        print("No matching open feature to close.")


def _confirm() -> bool:
    # A pipe containing "yes" is not human confirmation. CI must opt in with
    # --yes, which still requires the caller to choose --feature or --all.
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise ValueError("Confirmation requires a terminal; use --yes for unattended operation")
    print("This closes the selected features; their ledgers stay on disk.", file=sys.stderr)
    print("Type 'yes' to confirm: ", end="", file=sys.stderr, flush=True)
    return sys.stdin.readline().strip() == "yes"


def main() -> None:
    args = _parser().parse_args()
    try:
        plan = plan_reset(feature_id=args.feature, all_features=args.all)
        _show_plan(plan)
        if args.dry_run:
            print("Dry run: no feature closed.")
            return
        if not plan.will_change:
            print("Nothing to close; state unchanged.")
            return
        if not args.yes and not _confirm():
            print("Reset cancelled; state unchanged.", file=sys.stderr)
            raise SystemExit(1)

        # apply_reset compares the entire preview (including exact state
        # digest) under the shared lock before it performs an atomic write.
        applied = apply_reset(plan)
        print(
            f"Closed {len(applied.affected_features)} feature(s) "
            f"and {applied.total_tasks} task/issue record(s) (ledgers retained)."
        )
    except (ValueError, OSError) as exc:
        print(f"scaffold clean: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        print("\nReset cancelled; state unchanged.", file=sys.stderr)
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
