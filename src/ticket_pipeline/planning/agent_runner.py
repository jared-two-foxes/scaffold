"""
agent_runner - terminal-aware agent loop for AgentPlanningStrategy.

Extends the run_with_tools concept with explicit terminal-tool semantics:
certain tool calls end the loop immediately rather than feeding results
back to the model. Plain-text responses (no tool call) are protocol
violations. The existing run_with_tools() and all its callers are
unchanged.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

from ..lib import ai_client
from ..lib.ai_client import AIError, StepBudgetExceeded, _post_chat_completion
from ..lib import tools as tool_lib
from .agent_tools import (
    SUBMIT_PLAN_TOOL_NAME,
    PLANNING_FAILED_TOOL_NAME,
    ASK_USER_INPUT_TOOL_NAME,
    TERMINAL_TOOL_NAMES,
    summarize_agent_tool_call,
)
from .strategy import PlanningError

log = logging.getLogger(__name__)


class PlanningInputRequired(PlanningError):
    """
    Raised when the agent calls ask_user_input in 'fail' mode.
    Surfaces the question for external handling.
    """

    def __init__(self, question: str, why_needed: str = "") -> None:
        self.question = question
        self.why_needed = why_needed
        super().__init__(
            f"Planning requires user input: {question}"
            + (f" (why: {why_needed})" if why_needed else "")
        )


@dataclass
class TerminalToolResult:
    """Outcome of a terminal tool call."""

    tool_name: str
    arguments: dict
    turn_count: int


def run_agent_until_terminal(
    *,
    prompt: str,
    tools: list[dict],
    executor: Callable[[str, dict], str | None],
    terminal_tools: frozenset[str],
    model: str,
    max_turns: int,
    label: str = "agent-plan",
) -> TerminalToolResult:
    """
    Run the agent loop until a terminal tool is called.

    Unlike run_with_tools(), this function:
    - Treats calls to `terminal_tools` as loop-ending signals
    - Raises PlanningError for plain-text responses (protocol violation)
    - Allows one corrective prompt on plain-text before raising

    The executor may return None to indicate it handled the tool call
    internally (e.g. for interactive user input that was answered).
    A non-None return is fed back to the model as a tool message.
    """
    max_cost_usd = ai_client._load_max_cost_usd()
    messages: list[dict] = [{"role": "user", "content": prompt}]

    provider, _ = ai_client.resolve_provider(model)
    log.info("\n-- Running %r via %s (model=%s) ...", label, provider.base_url, model)

    turn = 0
    protocol_violation_sent = False

    while True:
        turn += 1
        if turn > max_turns:
            msg = f"{label}: exceeded {max_turns} turns with no terminal tool call - aborting."
            log.critical(msg)
            raise StepBudgetExceeded(msg)

        parsed = _post_chat_completion(
            {"model": model, "messages": messages, "tools": tools}, label
        )

        if max_cost_usd is not None:
            cost_so_far, _unpriced = ai_client.usage.total_cost_usd()
            if cost_so_far >= max_cost_usd:
                msg = (
                    f"{label}: cumulative cost ~${cost_so_far:.4f} reached the "
                    f"${max_cost_usd:.4f} ceiling - aborting."
                )
                log.critical(msg)
                raise StepBudgetExceeded(msg)

        try:
            message = parsed["choices"][0]["message"]
        except (KeyError, IndexError) as e:
            raise AIError(f"{label}: unexpected response shape: {parsed}") from e

        messages.append(message)
        tool_calls = message.get("tool_calls")

        if not tool_calls:
            # Plain-text response is a protocol violation.
            if not protocol_violation_sent:
                protocol_violation_sent = True
                violation_note = (
                    "Protocol violation: you must call either submit_plan or "
                    "planning_failed to end this session - plain text responses "
                    "are not accepted. If planning is complete, call submit_plan "
                    "now. If it cannot proceed, call planning_failed."
                )
                log.warning(
                    "   %s: plain-text response received - sending corrective prompt.",
                    label,
                )
                messages.append({"role": "user", "content": violation_note})
                continue
            raise PlanningError(
                f"{label}: agent produced a plain-text response twice without "
                "calling a terminal tool (submit_plan or planning_failed). "
                "Protocol violation."
            )

        # Reset violation flag when tool calls are seen.
        protocol_violation_sent = False

        terminal_result: TerminalToolResult | None = None

        for call in tool_calls:
            function = call.get("function", {})
            name = function.get("name", "")
            try:
                args = json.loads(function.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}

            log.debug("   %s", summarize_agent_tool_call(name, args))

            if name in terminal_tools:
                # Terminal tool - capture result, stop after this call.
                terminal_result = TerminalToolResult(
                    tool_name=name,
                    arguments=args,
                    turn_count=turn,
                )
                # Don't process any further tool calls in this batch.
                break

            result_text = executor(name, args)
            if result_text is not None:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id", ""),
                        "content": result_text,
                    }
                )

        if terminal_result is not None:
            return terminal_result


def make_read_only_executor(
    user_input_mode: str = "interactive",
    preloaded_paths: set[str] | None = None,
) -> Callable[[str, dict], str]:
    """
    Build an executor that allows only read-only repository tools plus
    ask_user_input (handled per user_input_mode). Write operations are
    rejected.

    user_input_mode:
      'interactive' - print question and block on real stdin input
      'infer'       - tell agent to use recommended_option, record as assumption
      'fail'        - raise PlanningInputRequired
    """
    full_read_paths: set[str] = set(preloaded_paths or ())
    partial_ranges: set[tuple[str, int | None, int | None]] = set()

    def executor(name: str, args: dict) -> str:
        if name == ASK_USER_INPUT_TOOL_NAME:
            question = args.get("question", "(no question provided)")
            why_needed = args.get("why_needed", "")
            recommended = args.get("recommended_option", "")

            if user_input_mode == "fail":
                raise PlanningInputRequired(question, why_needed)

            if user_input_mode == "infer":
                choice = recommended or "(no recommendation provided)"
                log.info(
                    "   ask_user_input (infer mode): choosing recommended option: %s",
                    choice,
                )
                return (
                    f"No user available. Proceeding with recommended option: {choice}. "
                    f"Record this as an assumption in your submission."
                )

            # interactive mode
            print(f"\n? {question}")
            if why_needed:
                print(f"  (Why: {why_needed})")
            try:
                answer = input("> ").strip()
            except EOFError:
                answer = ""
            return (
                answer
                if answer
                else f"(no answer given - proceed with recommended option: {recommended or 'your best judgement'})"
            )

        try:
            if name == "read_file":
                path = args["path"]
                start_line = args.get("start_line")
                end_line = args.get("end_line")
                range_key = (path, start_line, end_line)

                if path in full_read_paths or range_key in partial_ranges:
                    return (
                        f'(duplicate read_file("{path}") - content already available '
                        f"from the initial prompt or an earlier read in this session; "
                        f"not re-sent to save context)"
                    )
                content = tool_lib.read_file(path, start_line, end_line)
                if start_line is None and end_line is None:
                    full_read_paths.add(path)
                else:
                    partial_ranges.add(range_key)
                return content

            if name == "list_dir":
                return tool_lib.list_dir(args.get("path", "."))

            if name == "search_files":
                return tool_lib.search_files(
                    args["pattern"],
                    args.get("path", "."),
                    args.get("regex", False),
                    args.get("max_results", tool_lib.DEFAULT_SEARCH_MAX_RESULTS),
                )

            # Forbidden write tools.
            if name in {"write_file", "edit_file", "delete_file", "run_command",
                        "apply_patch", "git_commit", "git_checkout"}:
                return (
                    f"ERROR: {name} is not available during planning - "
                    "the planning agent must not modify the repository. "
                    "Use read_file, list_dir, and search_files to inspect it."
                )

            return f"ERROR: unknown tool: {name}"

        except tool_lib.ToolError as e:
            return f"ERROR: {e}"
        except KeyError as e:
            return f"ERROR: missing required argument: {e}"

    return executor
