"""Direct implementation strategy."""

import sys

from .. import next_step
from ..lib import ai_client, render
from ..lib import implement as implement_lib
from ..lib import pipeline_lib as lib

PHASES = ["pending", "implemented", "awaiting-manual-impl", "done"]
IMPL_AWAITING_STATUS = "pending"
MANUAL_PENDING_STATUS = "awaiting-manual-impl"


def implement(frame, ctx, feedback=None, previous_changed_files=None):
    if frame.status not in ("pending", "implemented"):
        render.print_line(
            "-- Top frame is a direct-strategy criterion but its status "
            f"({frame.status!r}) isn't awaiting implementation. Run 'next_step' first."
        )
        sys.exit(1)

    render.print_line()
    render.print_line("-- Implementing directly (strategy=direct):")
    render.print_line(f"   Criterion: {frame.criterion}")

    changed_files = implement_lib.run_implement_direct_with_refine(
        frame,
        ctx.model,
        ctx.commands,
        ctx.max_attempts,
        retry_policy=ctx.retry_policy,
        feedback=feedback,
        previous_changed_files=previous_changed_files,
        allow_compile=ctx.allow_compile,
        compile_cmd=ctx.commands.get("build_cmd"),
        reset_on_retry=ctx.reset_on_retry,
        test_commit_sha=frame.test_commit_sha,
    )

    render.print_line()
    render.print_line(f"-- Implemented: {frame.criterion}")
    render.print_line(f"   Files changed ({len(changed_files)}): {', '.join(changed_files)}")
    render.print_line(f"-- Token usage: {ai_client.usage}")
    return changed_files


def do_await_impl(frame):
    render.print_line()
    render.print_line("-- Manual implementation required (--skip-implementation is set):")
    render.print_line(f"   Criterion: {frame.criterion}")
    plan_context = frame.plan_context or ""
    if plan_context:
        render.print_line("   Plan context:")
        render.print_line(f"   {plan_context}")
    paths = lib.extract_referenced_paths(f"{frame.criterion}\n{plan_context}")
    if paths:
        render.print_line(f"   Referenced files: {', '.join(paths)}")
    render.print_line("   Implement it by hand, then run 'next_step' to re-check.")
    sys.exit(0)


def recheck(stack, frame, ctx):
    paths = lib.extract_referenced_paths(f"{frame.criterion}\n{frame.plan_context}")
    mechanically_confirmed = bool(paths) and bool(set(paths) & set(lib.git_changed_files()))
    if mechanically_confirmed or ctx.accept_manual:
        frame.status = "done"
        lib.save_stack(stack)
        return

    render.print_line()
    render.print_line(
        "-- Implementation complete but not yet verified: no referenced file "
        "appears in git changes."
    )
    render.print_line(f"   Criterion: {frame.criterion}")
    render.print_line(
        "   Make the change by hand, or run 'next_step --accept-manual' to confirm it's done."
    )
    sys.exit(0)


def advance(stack, frame, ctx):
    if frame.status == "pending":
        next_step._record_base_commit_if_needed(stack, frame, ctx.git_cfg)
        if ctx.skip_implementation:
            frame.status = MANUAL_PENDING_STATUS
            lib.save_stack(stack)
            do_await_impl(frame)
            return
        frame.status = "implemented"
        implement(frame, ctx)
        lib.save_stack(stack)
        if ctx.continuous:
            return
        sys.exit(0)

    if frame.status == "implemented":
        recheck(stack, frame, ctx)
        return

    if frame.status == MANUAL_PENDING_STATUS:
        recheck(stack, frame, ctx)
        return

    sys.exit(0)
