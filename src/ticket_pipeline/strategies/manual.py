"""Manual verification strategy handler."""

import sys

from .. import next_step
from ..lib import pipeline_lib as lib

PHASES = ["pending", "manual-pending", "done"]


def advance(stack, frame, ctx):
    if frame.status == "pending":
        next_step.do_manual_criterion(stack, frame, ctx.accept_manual, ctx.git_cfg)
        return

    if frame.status == next_step.MANUAL_PENDING_STATUS:
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
