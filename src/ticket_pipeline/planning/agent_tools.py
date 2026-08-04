"""
agent_tools - terminal pseudo-tool definitions for AgentPlanningStrategy.

Defines the JSON schemas and executor logic for the three special tools
the planning agent uses to end its session:

  submit_plan    - successful completion (the only valid exit)
  planning_failed - explicit failure termination
  ask_user_input  - interactive question (non-terminal when answered)

These are intentionally separate from the read-only repository tools.
"""

from __future__ import annotations

from ..lib.tools import (
    LIST_DIR_SCHEMA,
    READ_FILE_SCHEMA,
    SEARCH_FILES_SCHEMA,
)

# ---------------------------------------------------------------------------
# Schema constants
# ---------------------------------------------------------------------------

SUBMIT_PLAN_TOOL_NAME = "submit_plan"
PLANNING_FAILED_TOOL_NAME = "planning_failed"
ASK_USER_INPUT_TOOL_NAME = "ask_user_input"

SUBMIT_PLAN_SCHEMA = {
    "type": "function",
    "function": {
        "name": SUBMIT_PLAN_TOOL_NAME,
        "description": (
            "Submit your completed planning result. This is the ONLY way to "
            "successfully finish the planning session. Call this once you have "
            "assessed every acceptance criterion, gathered sufficient evidence, "
            "and designed the remaining changes. The submission must cover every "
            "criterion exactly once. Do not call this until you have verified: "
            "(1) every criterion has exactly one assessment; "
            "(2) satisfied claims have concrete evidence with repository paths; "
            "(3) remaining criteria have actionable planned changes, verification "
            "mode, and implementation strategy; "
            "(4) all mentioned paths exist in the repository or are explicitly "
            "new paths grounded in the surrounding structure."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ticket_summary": {
                    "type": "string",
                    "description": "Concise summary of what the ticket requires.",
                },
                "approach_summary": {
                    "type": "string",
                    "description": "High-level description of the overall implementation approach.",
                },
                "assumptions": {
                    "type": "array",
                    "description": "Material assumptions made during planning.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {"type": "string"},
                            "answer": {"type": "string"},
                            "basis": {"type": "string"},
                        },
                        "required": ["question", "answer", "basis"],
                    },
                },
                "repository_findings": {
                    "type": "array",
                    "description": "Key observations about the repository relevant to this ticket.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "observation": {"type": "string"},
                        },
                        "required": ["observation"],
                    },
                },
                "criteria": {
                    "type": "array",
                    "description": "One assessment per acceptance criterion.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "criterion_id": {
                                "type": "string",
                                "description": (
                                    "The criterion ID assigned in the prompt (e.g. AC-1)."
                                ),
                            },
                            "source_criterion": {
                                "type": "string",
                                "description": "The original criterion text.",
                            },
                            "disposition": {
                                "type": "string",
                                "enum": [
                                    "remaining",
                                    "satisfied",
                                    "not_applicable",
                                    "blocked",
                                ],
                            },
                            "rationale": {
                                "type": "string",
                                "description": "Why this disposition was chosen.",
                            },
                            "evidence": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "path": {"type": "string"},
                                        "observation": {"type": "string"},
                                    },
                                    "required": ["observation"],
                                },
                            },
                            "planned_changes": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "path": {"type": "string"},
                                        "description": {"type": "string"},
                                        "symbols": {
                                            "type": "array",
                                            "items": {"type": "string"},
                                        },
                                    },
                                    "required": ["path", "description"],
                                },
                            },
                            "verification": {
                                "type": "string",
                                "enum": ["test", "test-refactor", "refactor", "manual"],
                            },
                            "implementation_strategy": {
                                "type": "string",
                                "enum": ["tdd", "direct", "refactor"],
                            },
                            "existing_test_refs": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Each entry must be in 'file::qualified_test_name' shape."
                                ),
                            },
                            "plan_context": {
                                "type": "string",
                                "description": "Additional context for downstream implementation.",
                            },
                            "blocker": {
                                "type": "string",
                                "description": "Required when disposition is 'blocked'.",
                            },
                        },
                        "required": [
                            "criterion_id",
                            "source_criterion",
                            "disposition",
                            "rationale",
                        ],
                    },
                },
                "cross_cutting_changes": {
                    "type": "array",
                    "description": "Changes that span multiple criteria.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "description": {"type": "string"},
                            "symbols": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "required": ["path", "description"],
                    },
                },
                "risks": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Implementation risks or open concerns.",
                },
                "validation_notes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Notes about the self-review performed before submission.",
                },
            },
            "required": [
                "ticket_summary",
                "approach_summary",
                "assumptions",
                "repository_findings",
                "criteria",
            ],
        },
    },
}

PLANNING_FAILED_SCHEMA = {
    "type": "function",
    "function": {
        "name": PLANNING_FAILED_TOOL_NAME,
        "description": (
            "Call this when planning cannot succeed and you cannot recover. "
            "Use this only when: the ticket is fundamentally unclear and "
            "ask_user_input is not available or was refused; the repository is "
            "inaccessible; requirements conflict irreconcilably; or a tool "
            "failure prevents required exploration. Do not use this as a shortcut "
            "when the problem is solvable with more exploration."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Clear explanation of why planning cannot proceed.",
                },
                "category": {
                    "type": "string",
                    "enum": [
                        "insufficient_ticket",
                        "repository_unavailable",
                        "unsupported_repository",
                        "conflicting_requirements",
                        "tool_failure",
                        "other",
                    ],
                },
                "recoverable": {
                    "type": "boolean",
                    "description": "Whether a human could fix the problem and retry.",
                },
                "suggested_action": {
                    "type": "string",
                    "description": "What the user should do to resolve this.",
                },
            },
            "required": ["reason", "category", "recoverable", "suggested_action"],
        },
    },
}

ASK_USER_INPUT_SCHEMA = {
    "type": "function",
    "function": {
        "name": ASK_USER_INPUT_TOOL_NAME,
        "description": (
            "Ask the user a single focused question when a material product or "
            "implementation decision cannot be resolved from the ticket, repository, "
            "or established conventions. This tool is NON-TERMINAL in interactive "
            "mode - the session continues after the answer. "
            "Do NOT use for low-risk internal implementation choices you can resolve "
            "yourself from the repository. Ask one question at a time."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The specific question to ask.",
                },
                "why_needed": {
                    "type": "string",
                    "description": "Why this cannot be resolved from available information.",
                },
                "options": {
                    "type": "array",
                    "description": "Possible answers with implications.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "implications": {"type": "string"},
                        },
                        "required": ["label"],
                    },
                },
                "recommended_option": {
                    "type": "string",
                    "description": "The option you would choose if the user defers to you.",
                },
            },
            "required": ["question", "why_needed"],
        },
    },
}

# ---------------------------------------------------------------------------
# Tool sets
# ---------------------------------------------------------------------------

# Read-only repository tools + terminal pseudo-tools + ask_user_input
AGENT_PLANNING_TOOLS: list[dict] = [
    READ_FILE_SCHEMA,
    LIST_DIR_SCHEMA,
    SEARCH_FILES_SCHEMA,
    SUBMIT_PLAN_SCHEMA,
    PLANNING_FAILED_SCHEMA,
    ASK_USER_INPUT_SCHEMA,
]

TERMINAL_TOOL_NAMES: frozenset[str] = frozenset({SUBMIT_PLAN_TOOL_NAME, PLANNING_FAILED_TOOL_NAME})


def summarize_agent_tool_call(name: str, args: dict) -> str:
    """One-line summary for console logging of agent tool calls."""
    from ..lib.tools import summarize_tool_call

    if name == SUBMIT_PLAN_TOOL_NAME:
        n = len(args.get("criteria", []))
        return f"submit_plan ({n} criteria)"
    if name == PLANNING_FAILED_TOOL_NAME:
        return f"planning_failed: {args.get('reason', '(no reason)')}"
    if name == ASK_USER_INPUT_TOOL_NAME:
        return f"ask_user_input: {args.get('question', '(no question)')}"
    return summarize_tool_call(name, args)
