"""Direct implementation strategy."""

import sys

from .. import implement_step, next_step
from ..lib import pipeline_lib as lib, render

PHASES = ["pending", "implemented", "done"]


def advance(stack, frame, ctx):
    if frame.status == "pending":
        next_step._record_base_commit_if_needed(stack, frame, ctx.git_cfg)
        changed_files = implement_step.run_implement_direct_with_refine(
            frame, ctx.model, ctx.commands, ctx.max_attempts
        )
        render.print_line()
        render.print_line(f"-- Implemented: {frame.criterion}")
        render.print_line(
            f"   Files changed ({len(changed_files)}): {', '.join(changed_files)}"
        )
        frame.status = "implemented"
        lib.save_stack(stack)
        if ctx.continuous:
            return
        sys.exit(0)

    if frame.status == "implemented":
        paths = lib.extract_referenced_paths(f"{frame.criterion}\n{frame.plan_context}")
        mechanically_confirmed = bool(paths) and bool(
            set(paths) & set(lib.git_changed_files())
        )
        if mechanically_confirmed:
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
        if ctx.accept_manual:
            frame.status = "done"
            lib.save_stack(stack)
            return
        sys.exit(0)
