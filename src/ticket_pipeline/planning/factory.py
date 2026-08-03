from __future__ import annotations

from pathlib import Path

from .strategies import AgentPlanningStrategy, MechanicalPlanningStrategy
from .strategy import PlanningError, PlanningStrategy

SUPPORTED_PLANNING_STRATEGIES = ("mechanical", "agent")


def validate_planning_strategy_name(name: str) -> str:
    normalized = name.strip().lower()
    if normalized not in SUPPORTED_PLANNING_STRATEGIES:
        raise PlanningError(
            f"Unknown planning strategy {name!r}.\n"
            f"Supported strategies: {', '.join(SUPPORTED_PLANNING_STRATEGIES)}."
        )
    return normalized


def load_agent_config(config_path: Path) -> dict:
    """
    Load [planning_agent] settings from a TOML config file.

    Supported keys: user_input, max_turns, max_invalid_submissions.
    Unknown keys fail clearly.
    """
    if not config_path.exists():
        return {}
    import tomllib

    with config_path.open("rb") as f:
        data = tomllib.load(f)
    section = data.get("planning_agent")
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise PlanningError(f"{config_path}: [planning_agent] must be a TOML table.")
    allowed = {"user_input", "max_turns", "max_invalid_submissions"}
    unknown = set(section) - allowed
    if unknown:
        raise PlanningError(
            f"{config_path}: [planning_agent] unknown key(s): {sorted(unknown)}. "
            f"Allowed: {sorted(allowed)}."
        )
    return section


def create_planning_strategy(
    name: str,
    config_path: Path | None = None,
) -> PlanningStrategy:
    normalized = validate_planning_strategy_name(name)
    if normalized == "mechanical":
        return MechanicalPlanningStrategy()

    # Load agent settings from config if available
    agent_cfg: dict = {}
    if config_path is not None:
        agent_cfg = load_agent_config(config_path)

    kwargs: dict = {}
    if "user_input" in agent_cfg:
        mode = agent_cfg["user_input"]
        if mode not in {"interactive", "infer", "fail"}:
            raise PlanningError(
                f"[planning_agent] user_input must be 'interactive', 'infer', or 'fail', "
                f"got {mode!r}."
            )
        kwargs["user_input_mode"] = mode
    if "max_turns" in agent_cfg:
        kwargs["max_turns"] = int(agent_cfg["max_turns"])
    if "max_invalid_submissions" in agent_cfg:
        kwargs["max_invalid_submissions"] = int(agent_cfg["max_invalid_submissions"])

    return AgentPlanningStrategy(**kwargs)
