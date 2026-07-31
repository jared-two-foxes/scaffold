"""Refactor strategy handler."""

import sys

from .. import next_step
from ..lib import pipeline_lib as lib

PHASES = ["pending", "baseline-confirmed", "done"]


def advance(stack, frame, ctx):
    if frame.status == "pending":
        next_step.do_refactor_setup(stack, frame, ctx.commands, ctx.git_cfg)
        return

    if frame.status == lib.BASELINE_CONFIRMED_STATUS:
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
