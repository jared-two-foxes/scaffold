"""Direct implementation strategy."""

import sys

from .. import next_step
from ..lib import ai_client, implement as implement_lib, pipeline_lib as lib, render

PHASES = ["pending", "implemented", "done"]
IMPL_AWAITING_STATUS = "pending"


def implement(frame, ctx, feedback=None, previous_changed_files=None):
    if frame.status not in ("pending", "implemented"):
        render.print_line(
            f"-- Top frame is a direct-strategy criterion but its status ({frame.status!r}) isn't awaiting implementation. Run 'next_step' first."
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
    render.print_line(
        f"   Files changed ({len(changed_files)}): {', '.join(changed_files)}"
    )
    render.print_line(f"-- Token usage: {ai_client.usage}")
    return changed_files


def recheck(stack, frame, ctx):
    paths = lib.extract_referenced_paths(f"{frame.criterion}\n{frame.plan_context}")
    mechanically_confirmed = bool(paths) and bool(
        set(paths) & set(lib.git_changed_files())
    )
    if mechanically_confirmed or ctx.accept_manual:
        frame.status = "done"
        lib.save_stack(stack)
        return

    render.print_line()
    render.print_line(
        "-- Implementation complete but not yet verified: no referenced file appears in git changes."
    )
    render.print_line(f"   Criterion: {frame.criterion}")
    render.print_line(
        "   Make the change by hand, or run 'next_step --accept-manual' to confirm it's done."
    )
    sys.exit(0)


def advance(stack, frame, ctx):
    if frame.status == "pending":
        next_step._record_base_commit_if_needed(stack, frame, ctx.git_cfg)
        frame.status = "implemented"
        implement(frame, ctx)
        lib.save_stack(stack)
        if ctx.continuous:
            return
        sys.exit(0)

    if frame.status == "implemented":
        recheck(stack, frame, ctx)
        return

    sys.exit(0)
