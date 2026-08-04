"""Backward-compatible alias for the direct implementation strategy.

Existing criterion stacks may still contain ``strategy: "manual"``.  Keep
this module available for those frames while using the direct strategy
implementation for all behavior.
"""

from .direct import IMPL_AWAITING_STATUS, PHASES, advance, implement, recheck

__all__ = [
    "PHASES",
    "IMPL_AWAITING_STATUS",
    "advance",
    "recheck",
    "implement",
]
