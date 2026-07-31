"""Refactor strategy handler."""

import sys

from .. import implement_step, next_step
from ..lib import ai_client, pipeline_lib as lib, render

PHASES = ["pending", "baseline-confirmed", "done"]
IMPL_AWAITING_STATUS = "baseline-confirmed"


def implement(frame, ctx, feedback=None, previous_changed_files=None):
    if frame.status != lib.BASELINE_CONFIRMED_STATUS:
        render.print_line(
            "-- Top frame is a refactor criterion but its status "
            + repr(frame.status)
            + " is not awaiting implementation. Run 'next_step' first to establish the baseline."
        )
        sys.exit(1)
    if not frame.test_files or not frame.test_names:
        render.print_line(
            "-- Refactor frame is baseline-confirmed but has no safety-net test(s) recorded. Run 'next_step' to re-run refactor setup."
        )
        sys.exit(1)

    green_results = lib.run_scoped_tests(
        frame.test_names, ctx.commands, "pre-refactor green check"
    )
    red_names = [
        n for n, r in zip(frame.test_names, green_results) if r.returncode != 0
    ]
    if red_names:
        render.print_line(
            "-- Safety-net test(s) are RED - the refactor cannot proceed until they are GREEN again (the safety net must hold before and after the refactor). Fix them first, then re-run."
        )
        for n in red_names:
            render.print_line("   RED: " + n)
        sys.exit(1)

    render.print_line()
    render.print_line("-- Refactoring (keeping safety-net tests GREEN):")
    for test_file, test_name in zip(frame.test_files, frame.test_names):
        render.print_line("   " + test_file + " :: " + test_name)
    render.print_line("   Criterion: " + frame.criterion)

    changed_files = implement_step.run_implement_with_refine(
        frame,
        ctx.model,
        ctx.commands,
        ctx.max_attempts,
        verification=frame.verification,
        feedback=feedback,
        previous_changed_files=previous_changed_files,
    )

    render.print_line()
    render.print_line("-- Refactored: " + frame.criterion)
    render.print_line(
        "   All " + str(len(frame.test_names)) + " safety-net test(s) still GREEN:"
    )
    for test_file, test_name in zip(frame.test_files, frame.test_names):
        render.print_line("     " + test_file + " :: " + test_name)
    render.print_line(
        "   Files changed ("
        + str(len(changed_files))
        + "): "
        + ", ".join(changed_files)
    )
    render.print_line("-- Token usage: " + str(ai_client.usage))
    return changed_files


def recheck(stack, frame, ctx):
    return next_step.recheck_refactor_tests(stack, frame, ctx.commands, ctx.git_cfg)


def advance(stack, frame, ctx):
    if frame.status == "pending":
        next_step.do_refactor_setup(stack, frame, ctx.commands, ctx.git_cfg)
        return

    if frame.status == lib.BASELINE_CONFIRMED_STATUS:
        results = lib.run_scoped_tests(
            frame.test_names, ctx.commands, "refactor pre-implement check", quiet=True
        )
        red_names = [n for n, r in zip(frame.test_names, results) if r.returncode != 0]
        paths = lib.extract_referenced_paths(f"{frame.criterion}\n{frame.plan_context}")
        if not red_names and (not paths or (set(paths) & set(lib.git_changed_files()))):
            frame.status = "done"
            lib.save_stack(stack)
            return

        implement(frame, ctx)
        if ctx.continuous:
            return
        sys.exit(0)

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
