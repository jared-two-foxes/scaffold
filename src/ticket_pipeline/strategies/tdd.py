"""TDD strategy handlers for criterion execution."""

import sys

from .. import implement_step
from .. import next_step
from ..lib import ai_client, pipeline_lib as lib, render

PHASES = ["pending", "test-written", "green-unconfirmed", "nothing-written", "done"]
IMPL_AWAITING_STATUS = "test-written"


def do_write_test(*args, **kwargs):
    return next_step.do_write_test(*args, **kwargs)


def _handle_no_test_written(*args, **kwargs):
    return next_step._handle_no_test_written(*args, **kwargs)


def recheck_test_frame(*args, **kwargs):
    return next_step.recheck_test_frame(*args, **kwargs)


def implement(frame, ctx, feedback=None, previous_changed_files=None):
    if frame.status != "test-written":
        render.print_line(
            f"-- Top frame is not awaiting implementation (status: {frame.status!r}). Run 'next_step' first."
        )
        sys.exit(1)
    if not frame.test_files or not frame.test_names:
        render.print_line(
            f"-- Top frame is not awaiting implementation (status: {frame.status!r}, no test recorded). Run 'next_step' first."
        )
        sys.exit(1)
    if frame.verification == "test-refactor":
        render.print_line(
            "-- This is a test-refactor criterion whose rewrite came back RED."
        )
        render.print_line(
            "   There is no production code to implement - the rewrite itself"
        )
        render.print_line("   is incorrect. Fix the test by hand (keep its assertions")
        render.print_line(
            "   functionally identical; change only the structural elements"
        )
        render.print_line(
            "   the criterion describes), then run 'next_step' to re-check."
        )
        sys.exit(1)

    red_results = lib.run_scoped_tests(
        frame.test_names, ctx.commands, "pre-implement red check"
    )
    still_red = [n for n, r in zip(frame.test_names, red_results) if r.returncode != 0]
    if not still_red:
        render.print_line(
            f"-- All {len(frame.test_names)} test(s) already green. Nothing to implement. Run 'next_step' to pop this criterion."
        )
        sys.exit(0)

    render.print_line()
    if len(frame.test_names) == 1:
        render.print_line("-- Implementing:")
    else:
        render.print_line(
            f"-- Implementing ({len(still_red)} of {len(frame.test_names)} still red):"
        )
    for test_file, test_name in zip(frame.test_files, frame.test_names):
        tag = "" if test_name in still_red else " (already passing)"
        render.print_line(f"   {test_file} :: {test_name}{tag}")
    render.print_line(f"   Criterion: {frame.criterion}")

    changed_files = implement_step.run_implement_with_refine(
        frame,
        ctx.model,
        ctx.commands,
        ctx.max_attempts,
        feedback=feedback,
        previous_changed_files=previous_changed_files,
    )

    render.print_line()
    render.print_line(f"-- Implemented: {frame.criterion}")
    if len(frame.test_names) == 1:
        render.print_line(
            f"   Test now green: {frame.test_files[0]} :: {frame.test_names[0]}"
        )
    else:
        render.print_line(f"   All {len(frame.test_names)} test(s) now green:")
        for test_file, test_name in zip(frame.test_files, frame.test_names):
            render.print_line(f"     {test_file} :: {test_name}")
    render.print_line(
        f"   Files changed ({len(changed_files)}): {', '.join(changed_files)}"
    )
    render.print_line(f"-- Token usage: {ai_client.usage}")


def recheck(stack, frame, ctx):
    return recheck_test_frame(
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
