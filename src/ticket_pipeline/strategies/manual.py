"""Manual verification strategy handler."""

import sys

from .. import next_step
from ..lib import ai_client, render
from ..lib import implement as implement_lib
from ..lib import pipeline_lib as lib

PHASES = ["pending", "manual-pending", "done"]
MANUAL_PENDING_STATUS = "awaiting-manual-impl"

IMPL_AWAITING_STATUS = "awaiting-manual-impl"


def do_await_manual_impl(frame: "lib.CriterionFrame", paths: list[str]) -> None:
    render.print_line()
    render.print_line("-- Manual change needed (not test-verifiable):")
    render.print_line(f"   Criterion: {frame.criterion}")
    if frame.plan_context:
        render.print_line(f"   Context: {frame.plan_context}")
    if paths:
        render.print_line(f"   Expecting changes to: {', '.join(paths)}")
        render.print_line(
            "   Make the change by hand, or run 'next_step' again to let the "
            "pipeline attempt it automatically. A later 'next_step' run checks "
            "whether those file(s) actually changed before marking this done."
        )
    else:
        render.print_line(
            "   No specific file could be identified from this criterion, so "
            "there's nothing to mechanically check here. Make the change by "
            "hand, or run 'next_step' again to let the pipeline try it, then "
            "use 'next_step --accept-manual' to confirm it's done."
        )
    render.print_line(f"-- Token usage: {ai_client.usage}")
    sys.exit(0)


def do_manual_criterion(
    stack: list,
    frame: "lib.CriterionFrame",
    accept_manual: bool,
    git_cfg: "lib.GitConfig | None" = None,
) -> None:
    next_step._record_base_commit_if_needed(stack, frame, git_cfg)
    paths = lib.extract_referenced_paths(f"{frame.criterion}\n{frame.plan_context}")
    mechanically_confirmed = bool(paths) and bool(set(paths) & set(lib.git_changed_files()))
    if mechanically_confirmed or accept_manual:
        frame.status = "done"
        lib.save_stack(stack)
        return
    frame.status = MANUAL_PENDING_STATUS
    lib.save_stack(stack)
    do_await_manual_impl(frame, paths)


def implement(frame, ctx, feedback=None, previous_changed_files=None):
    if frame.status not in ("pending", "awaiting-manual-impl"):
        render.print_line(
            "-- Top frame is a manual-verification criterion but its status "
            f"({frame.status!r}) isn't awaiting implementation. Run 'next_step' "
            "first."
        )
        sys.exit(1)

    render.print_line()
    render.print_line("-- Implementing directly (verification=manual, no target test):")
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


def recheck(stack, frame, ctx):
    return do_manual_criterion(stack, frame, ctx.accept_manual, ctx.git_cfg)


def advance(stack, frame, ctx):
    if frame.status == "pending":
        do_manual_criterion(stack, frame, ctx.accept_manual, ctx.git_cfg)
        return

    if frame.status == MANUAL_PENDING_STATUS:
        do_manual_criterion(stack, frame, ctx.accept_manual, ctx.git_cfg)
        return

    if frame.status == "done":
        next_step.do_pop(frame, ctx)
        return

    sys.exit(0)
