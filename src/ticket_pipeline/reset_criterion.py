#!/usr/bin/env python3
"""
reset-criterion - roll the top criterion back to its pre-WRITE_TEST state
and `git reset --hard` the working tree to the commit recorded when that
criterion's test-writing began. The git-native counterpart to manually
editing .criteria-stack.json: instead of hand-clearing a frame's
test_files/test_names and hoping the on-disk code matches, this makes
the stack and the working tree consistent in one step.

Only operates on the *top* frame (the one `next_step` would dispatch
next) - resetting a frame buried under others would desync the
per-criterion commits above it from the code they were supposed to
capture, so it's refused outright.

Requires `git_workflow = true` in .dev-pipeline.toml: the whole
mechanism depends on base_commit having been recorded at WRITE_TEST
time, which only happens in git-workflow mode. Outside that mode there's
no git state to reset and no base_commit to reset to - refuse rather
than half-work.

The `git reset --hard <base_commit>` only rolls back to before *this*
criterion's WRITE_TEST - prior criteria's commits (recorded on POP) are
preserved, since each of those is an ancestor of this frame's
base_commit. Pipeline state files (.criteria-stack.json,
.pipeline-git-state.json, ...) are gitignored (see
lib.ensure_gitignore_entries) so the reset never destroys the stack
itself - only the code changes this criterion introduced.

After reset, the frame is returned to status="pending" with its
test_files/test_names/unconfirmed_tests/base_commit/commit_sha cleared,
so the next `next_step` call re-enters WRITE_TEST fresh - new test, new
base_commit, a clean re-do of just this criterion.

Usage:
    reset-criterion [--config <path>] [--log-level <level>]
"""

import argparse
import sys
from pathlib import Path

from .lib import pipeline_lib as lib, render, verbosity

log = verbosity.get_logger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reset the top criterion: git reset --hard to its "
                     "pre-WRITE_TEST commit and return the frame to pending.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
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
    args = parser.parse_args()
    verbosity.setup_logging(args.log_level)

    config_path = Path(args.config)
    git_cfg = lib.load_git_config(config_path)
    if not git_cfg.git_workflow:
        render.print_line(
            "-- reset-criterion requires git_workflow = true in "
            f"{config_path}. Outside git-workflow mode there is no "
            "base_commit to reset to."
        )
        sys.exit(1)

    if not lib.git_is_repo():
        lib.die("git_workflow is on but the current directory is not a git repository.")

    stack = lib.load_stack()
    if not stack:
        render.print_line("-- No work remaining. Stack is empty.")
        sys.exit(0)

    frame = stack[0]
    if frame.status == lib.VALIDATING_STATUS:
        lib.die(
            "The top frame is a TICKET_VALIDATE sentinel, not a criterion. "
            "reset-criterion rolls back a single criterion's test-writing; "
            "run 'next_step' to (re)run validation instead."
        )

    if frame.base_commit is None:
        lib.die(
            f"No base_commit recorded for the top criterion of {frame.ticket} "
            "- it has not entered WRITE_TEST yet (or git_workflow was off when "
            "it did). reset-criterion can only roll back a criterion that has "
            "started test writing. Run 'next_step' to begin it first."
        )

    render.print_line(f"-- Resetting top criterion of {frame.ticket}:")
    render.print_line(f"   {frame.criterion}")
    render.print_line(f"-- git reset --hard {frame.base_commit}")

    try:
        lib.git_reset_hard(frame.base_commit)
    except lib.GitError as e:
        lib.die(f"git reset --hard failed: {e}")

    frame.status = "pending"
    frame.test_files = None
    frame.test_names = None
    frame.unconfirmed_tests = []
    frame.base_commit = None
    frame.commit_sha = None
    lib.save_stack(stack)

    render.print_line("-- Criterion reset to pending. Working tree restored to pre-WRITE_TEST state.")
    render.print_line("-- Run 'next_step' to re-enter WRITE_TEST for this criterion.")


if __name__ == "__main__":
    main()