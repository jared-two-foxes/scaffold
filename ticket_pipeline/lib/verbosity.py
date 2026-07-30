"""
verbosity - process-wide leveled logging setup, shared by ai_client.py
and pipeline_lib.py (neither imports the other - ai_client.py is the
lower-level, dependency-free module - so this lives in its own leaf
module both can import without creating a cycle).

Built on stdlib logging rather than a hand-rolled level system: level
filtering and stream routing are already correct and battle-tested
there. The one thing it doesn't have out of the box is a level below
DEBUG - added the standard way (addLevelName + a Logger.trace method),
so TRACE slots into the existing ladder cleanly.

Deliberately NOT named logging.py - it's a real submodule name
(ticket_pipeline.lib.verbosity) rather than a shadowing risk now, but
the name stays: an import of "logging" from inside this package should
mean the stdlib module, not this one.
"""

import logging
import sys

TRACE = 5
logging.addLevelName(TRACE, "TRACE")


def _trace(self: logging.Logger, message: str, *args, **kwargs) -> None:
    if self.isEnabledFor(TRACE):
        self._log(TRACE, message, args, **kwargs)


logging.Logger.trace = _trace  # type: ignore[attr-defined]

LEVELS: dict[str, int] = {
    "trace": TRACE,
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}


class _MaxLevelFilter(logging.Filter):
    """Caps a handler at `max_level` inclusive - used to keep WARNING+
    off stdout once the stderr handler is already covering it, so
    nothing gets printed twice."""

    def __init__(self, max_level: int) -> None:
        super().__init__()
        self.max_level = max_level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno <= self.max_level


def setup_logging(level_name: str = "info") -> None:
    """
    Configure the root logger for this process. Bare '%(message)s'
    formatting - no timestamps/logger-name clutter by default, matching
    the unadorned print() style every script used before this module
    existed. Split across two handlers so die()/warnings keep landing on
    stderr (existing behavior) while everything else goes to stdout,
    both independently level-filtered rather than one stream for
    everything.
    """
    level = LEVELS[level_name.lower()]
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    formatter = logging.Formatter("%(message)s")

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    stdout_handler.setLevel(level)
    stdout_handler.addFilter(_MaxLevelFilter(logging.INFO))
    root.addHandler(stdout_handler)

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)
    stderr_handler.setLevel(max(level, logging.WARNING))
    root.addHandler(stderr_handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
