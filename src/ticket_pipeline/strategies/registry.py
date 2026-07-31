"""Strategy registry: maps strategy names to handler modules."""

from importlib import import_module

from ..lib import pipeline_lib as lib

_REGISTRY: dict[str, str] = {
    "tdd": "ticket_pipeline.strategies.tdd",
    "direct": "ticket_pipeline.strategies.direct",
    "manual": "ticket_pipeline.strategies.manual",
    "refactor": "ticket_pipeline.strategies.refactor",
}


def resolve_strategy(frame: "lib.CriterionFrame"):
    """Return the strategy module for frame.strategy."""
    module_path = _REGISTRY.get(frame.strategy)
    if module_path is None:
        lib.die_with_log(
            "strategy",
            f"Unknown strategy {frame.strategy!r}. Known strategies: {', '.join(sorted(_REGISTRY))}.",
            criterion=frame.criterion,
            ticket=frame.ticket,
        )
    return import_module(module_path)
