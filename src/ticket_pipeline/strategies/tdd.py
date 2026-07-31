"""TDD strategy handlers for criterion execution."""

import sys

from .. import next_step
from ..lib import pipeline_lib as lib

PHASES = ["pending", "test-written", "green-unconfirmed", "nothing-written", "done"]


def do_write_test(*args, **kwargs):
    return next_step.do_write_test(*args, **kwargs)


def _handle_no_test_written(*args, **kwargs):
    return next_step._handle_no_test_written(*args, **kwargs)


def recheck_test_frame(*args, **kwargs):
    return next_step.recheck_test_frame(*args, **kwargs)


def advance(stack, frame, ctx):
    """TDD strategy dispatch: test-first -> implement -> verify green."""
    if frame.status == next_step.GREEN_UNCONFIRMED_STATUS:
        recheck_test_frame(
            stack,
            frame,
            ctx.model,
            ctx.commands,
            ctx.accept_green,
            ctx.continuous,
            ctx.max_attempts,
            ctx.skip_implementation,
            ctx.git_cfg,
        )
        return

    if frame.status == next_step.NOTHING_WRITTEN_STATUS:
        _handle_no_test_written(
            stack,
            frame,
            ctx.model,
            ctx.accept_no_test,
            skip_ai=True,
        )
        return

    if frame.status == "pending":
        if frame.verification == "test-refactor" and lib.check_test_refactor_satisfied(
            frame.criterion, frame.existing_test_refs
        ):
            frame.status = "done"
            frame.unconfirmed_tests = []
            lib.save_stack(stack)
            return
        do_write_test(
            stack,
            frame,
            ctx.model,
            ctx.commands,
            accept_no_test=ctx.accept_no_test,
            skip_implementation=ctx.skip_implementation,
            continuous=ctx.continuous,
            max_attempts=ctx.max_attempts,
            accept_green=ctx.accept_green,
            git_cfg=ctx.git_cfg,
        )
        return

    if frame.status == "test-written":
        if not frame.test_files or not frame.test_names:
            do_write_test(
                stack,
                frame,
                ctx.model,
                ctx.commands,
                accept_no_test=ctx.accept_no_test,
                skip_implementation=ctx.skip_implementation,
                continuous=ctx.continuous,
                max_attempts=ctx.max_attempts,
                accept_green=ctx.accept_green,
                git_cfg=ctx.git_cfg,
            )
            return
        recheck_test_frame(
            stack,
            frame,
            ctx.model,
            ctx.commands,
            ctx.accept_green,
            ctx.continuous,
            ctx.max_attempts,
            ctx.skip_implementation,
            ctx.git_cfg,
        )
        return

    if frame.verification == "manual" and frame.status == "pending":
        next_step.do_manual_criterion(stack, frame, ctx.accept_manual, ctx.git_cfg)
        return

    if (
        frame.verification == "manual"
        and frame.status == next_step.MANUAL_PENDING_STATUS
    ):
        next_step._run_implementation_phase(
            stack,
            frame,
            ctx.model,
            ctx.commands,
            ctx.continuous,
            ctx.max_attempts,
            ctx.accept_green,
            ctx.accept_manual,
            ctx.git_cfg,
        )
        return

    if frame.verification == "refactor" and frame.status == "pending":
        next_step.do_refactor_setup(stack, frame, ctx.commands, ctx.git_cfg)
        return

    if (
        frame.verification == "refactor"
        and frame.status == lib.BASELINE_CONFIRMED_STATUS
    ):
        next_step._run_implementation_phase(
            stack,
            frame,
            ctx.model,
            ctx.commands,
            ctx.continuous,
            ctx.max_attempts,
            ctx.accept_green,
            ctx.accept_manual,
            ctx.git_cfg,
        )
        return

    if frame.status == "done":
        next_step.do_pop(
            frame,
            ctx.continuous,
            ctx.model,
            {},
            ctx.commands,
            ctx.config_path,
            ctx.git_cfg,
        )
        return

    sys.exit(0)
