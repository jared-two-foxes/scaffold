#!/usr/bin/env python3
"""
give-feedback - attach user feedback to the top criterion so the next
`next-step` run can treat it as a first-class retry path instead of a
manual interruption.

The feedback is always scoped to the current top frame only. Target
selection is routed through the frame's verification mode:
  - test-refactor -> tester
  - refactor      -> implementor
  - manual        -> human-only (no automated retry path yet)
  - test          -> tester or implementor, depending on --target/phase

Usage:
    give-feedback [--target auto|tester|implementor|human]
                  [--config <path>] [--log-level <level>]
                  <feedback...>
"""

import argparse
import sys
from pathlib import Path

from .lib import pipeline_lib as lib, render, verbosity


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Queue feedback on the top criterion for the next retry.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--target",
        default="auto",
        help="Retry target: auto (default), tester, implementor, or human.",
    )
    parser.add_argument(
        "--config",
        default=str(lib.PIPELINE_CONFIG_FILE),
        help=f"Path to the pipeline config (default: {lib.PIPELINE_CONFIG_FILE}).",
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=list(verbosity.LEVELS),
        help="Console verbosity (default: info).",
    )
    parser.add_argument("feedback", nargs="+", help="Feedback text to attach to the top criterion.")
    args = parser.parse_args()
    verbosity.setup_logging(args.log_level)

    stack = lib.load_stack()
    if not stack:
        render.print_line("-- No active ticket. Stack is empty.")
        sys.exit(1)

    frame = stack[0]
    if frame.status == lib.VALIDATING_STATUS:
        lib.die("The top frame is the validation sentinel; there is no criterion to redirect.")

    try:
        target = lib.resolve_feedback_target(frame, args.target)
    except ValueError as e:
        lib.die(str(e))

    git_cfg = lib.load_git_config(Path(args.config))
    if target == lib.FEEDBACK_TARGET_HUMAN:
        lib.die(
            f"verification={frame.verification!r} only supports human correction today; "
            "no automated feedback retry target is available."
        )
    if frame.feedback_attempts >= lib.FEEDBACK_MAX_RETRIES:
        lib.die(
            f"Feedback retry limit already reached ({lib.FEEDBACK_MAX_RETRIES}) for the top criterion."
        )
    if target == lib.FEEDBACK_TARGET_TESTER and (not git_cfg.git_workflow or frame.base_commit is None):
        lib.die(
            "Tester feedback requires git_workflow = true and a recorded base_commit so the "
            "previous test-writing attempt can be rolled back safely."
        )
    if target == lib.FEEDBACK_TARGET_IMPLEMENTOR:
        if frame.verification == "refactor" and frame.status != lib.BASELINE_CONFIRMED_STATUS:
            lib.die(
                "Implementor feedback for a refactor criterion requires the frame to be in the "
                "baseline-confirmed pause state first."
            )
        if frame.verification == "test" and (
            frame.status != "test-written" or not frame.test_files or not frame.test_names
        ):
            lib.die(
                "Implementor feedback for a test criterion requires a test-written frame with "
                "recorded test_files/test_names."
            )

    feedback = " ".join(args.feedback).strip()
    frame.feedback = feedback
    frame.feedback_target = target
    frame.status = lib.FEEDBACK_READY_STATUS
    lib.save_stack(stack)
    lib.log_feedback_event(
        "queued",
        feedback,
        criterion=frame.criterion,
        ticket=frame.ticket,
        target=target,
    )

    render.print_line(f"-- Queued feedback for {frame.ticket} ({target}).")
    render.print_line("-- Run 'scaffold next-step' to apply it.")


if __name__ == "__main__":
    main()
