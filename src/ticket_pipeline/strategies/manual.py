"""Manual verification strategy handler."""

import sys

from .. import implement_step, next_step
from ..lib import ai_client, pipeline_lib as lib, render

PHASES = ["pending", "manual-pending", "done"]

IMPL_AWAITING_STATUS = "awaiting-manual-impl"


def implement(frame, ctx, feedback=None, previous_changed_files=None):
    if frame.status not in ("pending", "awaiting-manual-impl"):
        render.print_line(
            f"-- Top frame is a manual-verification criterion but its status ({frame.status!r}) isn't awaiting implementation. Run 'next_step' first."
        )
        sys.exit(1)

    render.print_line()
    render.print_line("-- Implementing directly (verification=manual, no target test):")
    render.print_line(f"   Criterion: {frame.criterion}")

    changed_files = implement_step.run_implement_direct_with_refine(
        frame,
        ctx.model,
        ctx.commands,
        ctx.max_attempts,
        feedback=feedback,
        previous_changed_files=previous_changed_files,
    )

    render.print_line()
    render.print_line(f"-- Implemented: {frame.criterion}")
    render.print_line(
        f"   Files changed ({len(changed_files)}): {', '.join(changed_files)}"
    )
    render.print_line(f"-- Token usage: {ai_client.usage}")
    return changed_files


def recheck(stack, frame, ctx):
    return next_step.do_manual_criterion(stack, frame, ctx.accept_manual, ctx.git_cfg)


def advance(stack, frame, ctx):
    if frame.status == "pending":
        next_step._record_base_commit_if_needed(stack, frame, ctx.git_cfg)
        paths = lib.extract_referenced_paths(f"{frame.criterion}\n{frame.plan_context}")
        mechanically_confirmed = bool(paths) and bool(
            set(paths) & set(lib.git_changed_files())
        )
        if mechanically_confirmed or ctx.accept_manual:
            frame.status = "done"
            lib.save_stack(stack)
            return
        implement(frame, ctx)
        if ctx.continuous and paths:
            return
        sys.exit(0)

    if frame.status == next_step.MANUAL_PENDING_STATUS:
        paths = lib.extract_referenced_paths(f"{frame.criterion}\n{frame.plan_context}")
        mechanically_confirmed = bool(paths) and bool(
            set(paths) & set(lib.git_changed_files())
        )
        if mechanically_confirmed or ctx.accept_manual:
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
