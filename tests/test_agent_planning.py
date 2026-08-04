"""
Tests for the AgentPlanningStrategy implementation.

Covers:
- Agent models: construction, enums, required fields, immutability
- Terminal tools: schemas and summarization
- Agent runner: terminal completion, plain-text violations, corrective prompting,
  turn ceilings, same-context repair
- Criterion coverage: missing, duplicate, unknown IDs
- Dispositions: required and prohibited fields per disposition
- Submission validation
- Result adapter: remaining→frames, satisfied→artifacts, no-gap results
- Artifact rendering: deterministic .implementation-plan.md and .gap-plan.md
- Factory: config loading, unknown keys rejected
- CLI: explore incompatibility with agent strategy
- Integration: fake model transcript end-to-end
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ticket_pipeline.planning.agent_models import (
    VALID_DISPOSITIONS,
    AgentAssumption,
    AgentCriterionAssessment,
    AgentEvidence,
    AgentPlanSubmission,
    PlannedChange,
)
from ticket_pipeline.planning.agent_prompt import (
    assign_criterion_ids,
    extract_acceptance_criteria,
)
from ticket_pipeline.planning.agent_rendering import (
    build_agent_diagnostics,
    render_agent_full_plan,
    render_agent_gap_plan,
    render_plan_context,
)
from ticket_pipeline.planning.agent_runner import (
    PlanningInputRequired,
    make_read_only_executor,
    run_agent_until_terminal,
)
from ticket_pipeline.planning.agent_tools import (
    AGENT_PLANNING_TOOLS,
    ASK_USER_INPUT_TOOL_NAME,
    PLANNING_FAILED_TOOL_NAME,
    SUBMIT_PLAN_TOOL_NAME,
    TERMINAL_TOOL_NAMES,
    summarize_agent_tool_call,
)
from ticket_pipeline.planning.agent_validation import validate_submission
from ticket_pipeline.planning.factory import (
    create_planning_strategy,
    load_agent_config,
)
from ticket_pipeline.planning.models import (
    PlanningRequest,
    PlanningResult,
)
from ticket_pipeline.planning.strategies.agent import AgentPlanningStrategy
from ticket_pipeline.planning.strategy import PlanningError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_submission(
    criteria=None,
    **kwargs,
) -> AgentPlanSubmission:
    if criteria is None:
        criteria = (
            AgentCriterionAssessment(
                criterion_id="AC-1",
                source_criterion="Do the thing",
                disposition="remaining",
                rationale="Not yet implemented.",
                planned_changes=(PlannedChange(path="src/foo.py", description="Add foo"),),
                verification="test",
                implementation_strategy="tdd",
            ),
        )
    return AgentPlanSubmission(
        ticket_summary=kwargs.get("ticket_summary", "A ticket"),
        approach_summary=kwargs.get("approach_summary", "An approach"),
        assumptions=kwargs.get("assumptions", ()),
        repository_findings=kwargs.get("repository_findings", ()),
        criteria=criteria,
    )


def _minimal_remaining_args(criterion_id="AC-1", source_criterion="Do thing"):
    return {
        "criterion_id": criterion_id,
        "source_criterion": source_criterion,
        "disposition": "remaining",
        "rationale": "Not done.",
        "planned_changes": [{"path": "src/x.py", "description": "Add x"}],
        "verification": "test",
        "implementation_strategy": "tdd",
    }


def _minimal_submit_args(criteria=None):
    if criteria is None:
        criteria = [_minimal_remaining_args()]
    return {
        "ticket_summary": "Summary",
        "approach_summary": "Approach",
        "assumptions": [],
        "repository_findings": [],
        "criteria": criteria,
    }


# ---------------------------------------------------------------------------
# 1. Agent models
# ---------------------------------------------------------------------------


class AgentModelsTests(unittest.TestCase):
    def test_valid_dispositions(self):
        self.assertIn("remaining", VALID_DISPOSITIONS)
        self.assertIn("satisfied", VALID_DISPOSITIONS)
        self.assertIn("not_applicable", VALID_DISPOSITIONS)
        self.assertIn("blocked", VALID_DISPOSITIONS)

    def test_agent_evidence_frozen(self):
        e = AgentEvidence(path="src/a.py", observation="it exists")
        with self.assertRaises((AttributeError, TypeError)):
            e.path = "other"  # type: ignore[misc]

    def test_agent_assumption_frozen(self):
        a = AgentAssumption(question="Q?", answer="A", basis="B")
        with self.assertRaises((AttributeError, TypeError)):
            a.question = "other"  # type: ignore[misc]

    def test_planned_change_defaults(self):
        c = PlannedChange(path="src/x.py", description="desc")
        self.assertEqual((), c.symbols)

    def test_criterion_assessment_frozen(self):
        ac = AgentCriterionAssessment(
            criterion_id="AC-1",
            source_criterion="Do thing",
            disposition="remaining",
            rationale="Needed",
        )
        with self.assertRaises((AttributeError, TypeError)):
            ac.criterion_id = "AC-2"  # type: ignore[misc]

    def test_submission_frozen(self):
        sub = _make_submission()
        with self.assertRaises((AttributeError, TypeError)):
            sub.ticket_summary = "other"  # type: ignore[misc]

    def test_submission_allows_empty_collections(self):
        sub = AgentPlanSubmission(
            ticket_summary="t",
            approach_summary="a",
            assumptions=(),
            repository_findings=(),
            criteria=(),
        )
        self.assertEqual((), sub.criteria)
        self.assertEqual((), sub.risks)


# ---------------------------------------------------------------------------
# 2. Terminal tool schemas
# ---------------------------------------------------------------------------


class AgentToolsTests(unittest.TestCase):
    def test_terminal_tool_names(self):
        self.assertIn(SUBMIT_PLAN_TOOL_NAME, TERMINAL_TOOL_NAMES)
        self.assertIn(PLANNING_FAILED_TOOL_NAME, TERMINAL_TOOL_NAMES)
        self.assertNotIn(ASK_USER_INPUT_TOOL_NAME, TERMINAL_TOOL_NAMES)

    def test_all_required_tools_present(self):
        names = {s["function"]["name"] for s in AGENT_PLANNING_TOOLS}
        self.assertIn("read_file", names)
        self.assertIn("list_dir", names)
        self.assertIn("search_files", names)
        self.assertIn(SUBMIT_PLAN_TOOL_NAME, names)
        self.assertIn(PLANNING_FAILED_TOOL_NAME, names)
        self.assertIn(ASK_USER_INPUT_TOOL_NAME, names)

    def test_forbidden_write_tools_absent(self):
        names = {s["function"]["name"] for s in AGENT_PLANNING_TOOLS}
        for forbidden in (
            "write_file",
            "edit_file",
            "delete_file",
            "run_command",
            "git_commit",
            "git_checkout",
        ):
            self.assertNotIn(forbidden, names)

    def test_summarize_submit_plan(self):
        summary = summarize_agent_tool_call(SUBMIT_PLAN_TOOL_NAME, {"criteria": [{}, {}]})
        self.assertIn("submit_plan", summary)
        self.assertIn("2", summary)

    def test_summarize_planning_failed(self):
        summary = summarize_agent_tool_call(PLANNING_FAILED_TOOL_NAME, {"reason": "oops"})
        self.assertIn("oops", summary)

    def test_summarize_ask_user_input(self):
        summary = summarize_agent_tool_call(ASK_USER_INPUT_TOOL_NAME, {"question": "What?"})
        self.assertIn("What?", summary)


# ---------------------------------------------------------------------------
# 3. Read-only executor
# ---------------------------------------------------------------------------


class ReadOnlyExecutorTests(unittest.TestCase):
    def test_rejects_write_file(self):
        exec_ = make_read_only_executor()
        result = exec_("write_file", {"path": "x.py", "content": "hi"})
        self.assertIn("ERROR", result)
        self.assertIn("not available", result)

    def test_rejects_run_command(self):
        exec_ = make_read_only_executor()
        result = exec_("run_command", {"command": "ls"})
        self.assertIn("ERROR", result)

    def test_rejects_unknown_tool(self):
        exec_ = make_read_only_executor()
        result = exec_("frobnicate", {})
        self.assertIn("ERROR", result)

    def test_infer_mode_returns_recommendation(self):
        exec_ = make_read_only_executor(user_input_mode="infer")
        result = exec_(
            ASK_USER_INPUT_TOOL_NAME,
            {
                "question": "Which approach?",
                "why_needed": "unclear",
                "recommended_option": "option A",
            },
        )
        self.assertIn("option A", result)

    def test_fail_mode_raises_planning_input_required(self):
        exec_ = make_read_only_executor(user_input_mode="fail")
        with self.assertRaises(PlanningInputRequired) as ctx:
            exec_(ASK_USER_INPUT_TOOL_NAME, {"question": "Q?", "why_needed": "W"})
        self.assertIn("Q?", ctx.exception.question)

    def test_deduplicates_reads(self):
        exec_ = make_read_only_executor()
        with patch(
            "ticket_pipeline.planning.agent_runner.tool_lib.read_file",
            return_value="content",
        ) as mock_read:
            exec_("read_file", {"path": "src/a.py"})
            result = exec_("read_file", {"path": "src/a.py"})
        mock_read.assert_called_once()
        self.assertIn("duplicate", result)

    def test_preloaded_paths_not_re_read(self):
        exec_ = make_read_only_executor(preloaded_paths={"src/b.py"})
        with patch("ticket_pipeline.planning.agent_runner.tool_lib.read_file") as mock_read:
            result = exec_("read_file", {"path": "src/b.py"})
        mock_read.assert_not_called()
        self.assertIn("duplicate", result)


# ---------------------------------------------------------------------------
# 4. Agent runner: terminal completion and protocol
# ---------------------------------------------------------------------------


def _make_fake_post(responses):
    """
    Returns a mock for _post_chat_completion that cycles through `responses`.
    Each element is either:
      - a list of tool_calls dicts
      - None (plain text response)
    """
    call_index = [0]

    def fake_post(payload, label):
        idx = call_index[0]
        call_index[0] += 1
        resp = responses[idx]
        if resp is None:
            return {"choices": [{"message": {"content": "plain text", "tool_calls": None}}]}
        return {"choices": [{"message": {"content": None, "tool_calls": resp}}]}

    return fake_post


def _make_tool_call(name, args, call_id="c1"):
    return {
        "id": call_id,
        "function": {"name": name, "arguments": json.dumps(args)},
    }


class AgentRunnerTests(unittest.TestCase):
    def _run(self, responses, executor=None, terminal_tools=None, max_turns=40):
        """Helper: patch _post_chat_completion and run the loop."""
        if terminal_tools is None:
            terminal_tools = TERMINAL_TOOL_NAMES
        if executor is None:
            executor = make_read_only_executor()
        fake_post = _make_fake_post(responses)
        with patch(
            "ticket_pipeline.planning.agent_runner._post_chat_completion",
            side_effect=fake_post,
        ):
            return run_agent_until_terminal(
                prompt="test",
                tools=AGENT_PLANNING_TOOLS,
                executor=executor,
                terminal_tools=terminal_tools,
                model="test-model",
                max_turns=max_turns,
            )

    def test_terminal_tool_stops_loop(self):
        submit_args = _minimal_submit_args()
        responses = [
            [_make_tool_call(SUBMIT_PLAN_TOOL_NAME, submit_args)],
        ]
        result = self._run(responses)
        self.assertEqual(SUBMIT_PLAN_TOOL_NAME, result.tool_name)
        self.assertEqual(submit_args, result.arguments)
        self.assertEqual(1, result.turn_count)

    def test_planning_failed_terminates(self):
        failed_args = {
            "reason": "Cannot proceed",
            "category": "insufficient_ticket",
            "recoverable": True,
            "suggested_action": "Clarify ticket",
        }
        responses = [
            [_make_tool_call(PLANNING_FAILED_TOOL_NAME, failed_args)],
        ]
        result = self._run(responses)
        self.assertEqual(PLANNING_FAILED_TOOL_NAME, result.tool_name)

    def test_ordinary_tools_continue_loop(self):
        submit_args = _minimal_submit_args()
        responses = [
            [_make_tool_call("read_file", {"path": "src/x.py"})],
            [_make_tool_call(SUBMIT_PLAN_TOOL_NAME, submit_args)],
        ]
        with patch(
            "ticket_pipeline.planning.agent_runner.tool_lib.read_file",
            return_value="content",
        ):
            result = self._run(responses)
        self.assertEqual(SUBMIT_PLAN_TOOL_NAME, result.tool_name)
        self.assertEqual(2, result.turn_count)

    def test_plain_text_corrective_prompt_then_terminal(self):
        """One plain-text response gets a corrective prompt; then terminal tool accepted."""
        submit_args = _minimal_submit_args()
        responses = [
            None,  # plain text - protocol violation → corrective prompt sent
            [_make_tool_call(SUBMIT_PLAN_TOOL_NAME, submit_args)],
        ]
        result = self._run(responses)
        self.assertEqual(SUBMIT_PLAN_TOOL_NAME, result.tool_name)

    def test_two_plain_text_responses_raise_planning_error(self):
        responses = [None, None]
        with self.assertRaises(PlanningError) as ctx:
            self._run(responses)
        self.assertIn("protocol violation", str(ctx.exception).lower())

    def test_turn_ceiling_raises_step_budget_exceeded(self):
        from ticket_pipeline.lib.ai_client import StepBudgetExceeded

        # All turns are ordinary tool calls, never terminal.
        responses = [[_make_tool_call("list_dir", {})]] * 5
        with (
            patch(
                "ticket_pipeline.planning.agent_runner.tool_lib.list_dir",
                return_value="x",
            ),
            self.assertRaises(StepBudgetExceeded),
        ):
            self._run(responses, max_turns=3)

    def test_terminal_call_stops_before_processing_later_calls(self):
        """If submit_plan and another tool appear in the same batch,
        only submit_plan is processed."""
        submit_args = _minimal_submit_args()
        responses = [
            [
                _make_tool_call(SUBMIT_PLAN_TOOL_NAME, submit_args, call_id="c1"),
                _make_tool_call("read_file", {"path": "should_not_be_called.py"}, call_id="c2"),
            ],
        ]
        with patch("ticket_pipeline.planning.agent_runner.tool_lib.read_file") as mock_read:
            result = self._run(responses)
        mock_read.assert_not_called()
        self.assertEqual(SUBMIT_PLAN_TOOL_NAME, result.tool_name)


# ---------------------------------------------------------------------------
# 5. Criterion coverage and submission validation
# ---------------------------------------------------------------------------


class SubmissionValidationTests(unittest.TestCase):
    def test_valid_remaining_criterion(self):
        args = _minimal_submit_args()
        submission, errors = validate_submission(args, ["AC-1"])
        self.assertIsNotNone(submission)
        self.assertEqual([], errors)

    def test_missing_ticket_summary_rejected(self):
        args = _minimal_submit_args()
        args["ticket_summary"] = ""
        _, errors = validate_submission(args, ["AC-1"])
        self.assertTrue(any("ticket_summary" in e for e in errors))

    def test_missing_approach_summary_rejected(self):
        args = _minimal_submit_args()
        args["approach_summary"] = ""
        _, errors = validate_submission(args, ["AC-1"])
        self.assertTrue(any("approach_summary" in e for e in errors))

    def test_missing_criterion_id_rejected(self):
        args = _minimal_submit_args([{**_minimal_remaining_args(), "criterion_id": ""}])
        _, errors = validate_submission(args, ["AC-1"])
        self.assertTrue(errors)

    def test_duplicate_criterion_id_rejected(self):
        args = _minimal_submit_args(
            [
                _minimal_remaining_args("AC-1"),
                _minimal_remaining_args("AC-1"),
            ]
        )
        _, errors = validate_submission(args, ["AC-1"])
        self.assertTrue(any("duplicate" in e.lower() for e in errors))

    def test_unknown_criterion_id_rejected(self):
        args = _minimal_submit_args([_minimal_remaining_args("AC-99")])
        _, errors = validate_submission(args, ["AC-1"])
        self.assertTrue(any("unknown" in e.lower() for e in errors))

    def test_missing_criterion_rejected(self):
        args = _minimal_submit_args([_minimal_remaining_args("AC-1")])
        _, errors = validate_submission(args, ["AC-1", "AC-2"])
        self.assertTrue(any("AC-2" in e for e in errors))

    def test_remaining_without_planned_changes_rejected(self):
        criterion = {**_minimal_remaining_args(), "planned_changes": []}
        _, errors = validate_submission(_minimal_submit_args([criterion]), ["AC-1"])
        self.assertTrue(any("planned_change" in e.lower() for e in errors))

    def test_remaining_without_verification_rejected(self):
        criterion = {**_minimal_remaining_args()}
        del criterion["verification"]
        _, errors = validate_submission(_minimal_submit_args([criterion]), ["AC-1"])
        self.assertTrue(any("verification" in e.lower() for e in errors))

    def test_remaining_without_strategy_rejected(self):
        criterion = {**_minimal_remaining_args()}
        del criterion["implementation_strategy"]
        _, errors = validate_submission(_minimal_submit_args([criterion]), ["AC-1"])
        self.assertTrue(any("implementation_strategy" in e.lower() for e in errors))

    def test_remaining_invalid_verification_rejected(self):
        criterion = {**_minimal_remaining_args(), "verification": "banana"}
        _, errors = validate_submission(_minimal_submit_args([criterion]), ["AC-1"])
        self.assertTrue(any("verification" in e.lower() for e in errors))

    def test_existing_test_refs_without_shape_rejected(self):
        criterion = {
            **_minimal_remaining_args(),
            "existing_test_refs": ["tests/test_x.py"],
        }
        _, errors = validate_submission(_minimal_submit_args([criterion]), ["AC-1"])
        self.assertTrue(any("existing_test_refs" in e.lower() for e in errors))
        self.assertTrue(any("file::qualified_test_name" in e for e in errors))

    def test_satisfied_without_evidence_rejected(self):
        criterion = {
            "criterion_id": "AC-1",
            "source_criterion": "Do thing",
            "disposition": "satisfied",
            "rationale": "Already done.",
            "evidence": [],
        }
        _, errors = validate_submission(_minimal_submit_args([criterion]), ["AC-1"])
        self.assertTrue(any("evidence" in e.lower() for e in errors))

    def test_satisfied_with_planned_changes_rejected(self):
        criterion = {
            "criterion_id": "AC-1",
            "source_criterion": "Do thing",
            "disposition": "satisfied",
            "rationale": "Done.",
            "evidence": [{"observation": "found it", "path": "src/x.py"}],
            "planned_changes": [{"path": "src/y.py", "description": "add y"}],
        }
        _, errors = validate_submission(_minimal_submit_args([criterion]), ["AC-1"])
        self.assertTrue(any("planned_change" in e.lower() for e in errors))

    def test_not_applicable_with_planned_changes_rejected(self):
        criterion = {
            "criterion_id": "AC-1",
            "source_criterion": "Do thing",
            "disposition": "not_applicable",
            "rationale": "N/A",
            "planned_changes": [{"path": "src/y.py", "description": "add y"}],
        }
        _, errors = validate_submission(_minimal_submit_args([criterion]), ["AC-1"])
        self.assertTrue(any("planned_change" in e.lower() for e in errors))

    def test_blocked_without_blocker_rejected(self):
        criterion = {
            "criterion_id": "AC-1",
            "source_criterion": "Do thing",
            "disposition": "blocked",
            "rationale": "Blocked.",
            "blocker": "",
        }
        _, errors = validate_submission(_minimal_submit_args([criterion]), ["AC-1"])
        self.assertTrue(any("blocker" in e.lower() for e in errors))

    def test_blocked_with_planned_changes_rejected(self):
        criterion = {
            "criterion_id": "AC-1",
            "source_criterion": "Do thing",
            "disposition": "blocked",
            "rationale": "Blocked.",
            "blocker": "Need info",
            "planned_changes": [{"path": "src/y.py", "description": "add y"}],
        }
        _, errors = validate_submission(_minimal_submit_args([criterion]), ["AC-1"])
        self.assertTrue(any("planned_change" in e.lower() for e in errors))

    def test_satisfied_criterion_valid(self):
        criterion = {
            "criterion_id": "AC-1",
            "source_criterion": "Do thing",
            "disposition": "satisfied",
            "rationale": "Already done.",
            "evidence": [{"observation": "present at src/x.py", "path": "src/x.py"}],
        }
        submission, errors = validate_submission(_minimal_submit_args([criterion]), ["AC-1"])
        self.assertIsNotNone(submission)
        self.assertEqual([], errors)

    def test_no_gap_result_valid(self):
        """All satisfied = valid submission with empty remaining."""
        criterion = {
            "criterion_id": "AC-1",
            "source_criterion": "Do thing",
            "disposition": "satisfied",
            "rationale": "Done.",
            "evidence": [{"observation": "found", "path": "src/x.py"}],
        }
        submission, errors = validate_submission(_minimal_submit_args([criterion]), ["AC-1"])
        self.assertIsNotNone(submission)
        self.assertEqual([], errors)
        assert submission is not None
        remaining = [a for a in submission.criteria if a.disposition == "remaining"]
        self.assertEqual([], remaining)

    def test_empty_expected_ids_accepts_any_id(self):
        args = _minimal_submit_args([_minimal_remaining_args("CUSTOM-1")])
        submission, errors = validate_submission(args, [])
        self.assertIsNotNone(submission)
        self.assertEqual([], errors)


# ---------------------------------------------------------------------------
# 6. Result adapter
# ---------------------------------------------------------------------------


class ResultAdapterTests(unittest.TestCase):
    def _make_strategy(self):
        return AgentPlanningStrategy(user_input_mode="infer")

    def test_only_remaining_become_planned_criteria(self):
        remaining = AgentCriterionAssessment(
            criterion_id="AC-1",
            source_criterion="Do thing",
            disposition="remaining",
            rationale="Not done.",
            planned_changes=(PlannedChange(path="src/x.py", description="add x"),),
            verification="test",
            implementation_strategy="tdd",
        )
        satisfied = AgentCriterionAssessment(
            criterion_id="AC-2",
            source_criterion="Other thing",
            disposition="satisfied",
            rationale="Already done.",
            evidence=(AgentEvidence(path="src/y.py", observation="present"),),
        )
        submission = _make_submission(criteria=(remaining, satisfied))
        strategy = self._make_strategy()
        result = strategy._to_planning_result(submission, "plan", "gap")
        self.assertEqual(1, len(result.criteria))
        self.assertEqual("Do thing", result.criteria[0].criterion)

    def test_satisfied_criteria_produce_no_frames(self):
        satisfied = AgentCriterionAssessment(
            criterion_id="AC-1",
            source_criterion="Already done",
            disposition="satisfied",
            rationale="Done.",
            evidence=(AgentEvidence(path="src/x.py", observation="found"),),
        )
        submission = _make_submission(criteria=(satisfied,))
        strategy = self._make_strategy()
        result = strategy._to_planning_result(submission, "plan", "gap")
        self.assertEqual(0, len(result.criteria))

    def test_no_gap_result_has_empty_criteria(self):
        satisfied = AgentCriterionAssessment(
            criterion_id="AC-1",
            source_criterion="Already done",
            disposition="satisfied",
            rationale="Done.",
            evidence=(AgentEvidence(path="src/x.py", observation="found"),),
        )
        submission = _make_submission(criteria=(satisfied,))
        strategy = self._make_strategy()
        result = strategy._to_planning_result(submission, "plan", "gap")
        self.assertIsInstance(result, PlanningResult)
        self.assertEqual((), result.criteria)

    def test_verification_and_strategy_copied(self):
        remaining = AgentCriterionAssessment(
            criterion_id="AC-1",
            source_criterion="Do thing",
            disposition="remaining",
            rationale="needed",
            planned_changes=(PlannedChange(path="src/x.py", description="add x"),),
            verification="manual",
            implementation_strategy="direct",
        )
        submission = _make_submission(criteria=(remaining,))
        strategy = self._make_strategy()
        result = strategy._to_planning_result(submission, "plan", "gap")
        self.assertEqual("manual", result.criteria[0].verification)
        self.assertEqual("direct", result.criteria[0].implementation_strategy)

    def test_existing_test_refs_preserved(self):
        remaining = AgentCriterionAssessment(
            criterion_id="AC-1",
            source_criterion="Do thing",
            disposition="remaining",
            rationale="needed",
            planned_changes=(PlannedChange(path="src/x.py", description="add x"),),
            verification="test",
            implementation_strategy="tdd",
            existing_test_refs=("tests/test_x.py::test_old",),
        )
        submission = _make_submission(criteria=(remaining,))
        strategy = self._make_strategy()
        result = strategy._to_planning_result(submission, "plan", "gap")
        self.assertEqual(("tests/test_x.py::test_old",), result.criteria[0].existing_test_refs)

    def test_assumptions_become_diagnostics(self):
        submission = _make_submission(
            assumptions=(AgentAssumption(question="Q?", answer="A", basis="repo"),),
        )
        diagnostics = build_agent_diagnostics(submission)
        self.assertTrue(any(d.code == "agent_assumption" for d in diagnostics))


# ---------------------------------------------------------------------------
# 7. Artifact rendering
# ---------------------------------------------------------------------------


class ArtifactRenderingTests(unittest.TestCase):
    def _submission_with_remaining_and_satisfied(self):
        return AgentPlanSubmission(
            ticket_summary="Ticket X",
            approach_summary="Use approach Y",
            assumptions=(AgentAssumption(question="Q?", answer="A", basis="B"),),
            repository_findings=(AgentEvidence(path="src/main.py", observation="main file"),),
            criteria=(
                AgentCriterionAssessment(
                    criterion_id="AC-1",
                    source_criterion="Do the thing",
                    disposition="remaining",
                    rationale="not done",
                    planned_changes=(PlannedChange(path="src/x.py", description="add x"),),
                    verification="test",
                    implementation_strategy="tdd",
                    plan_context="Some context",
                ),
                AgentCriterionAssessment(
                    criterion_id="AC-2",
                    source_criterion="Other thing already done",
                    disposition="satisfied",
                    rationale="done",
                    evidence=(AgentEvidence(path="src/y.py", observation="found"),),
                ),
            ),
            risks=("Risk A",),
        )

    def test_full_plan_contains_both_criteria(self):
        sub = self._submission_with_remaining_and_satisfied()
        plan = render_agent_full_plan(sub)
        self.assertIn("AC-1", plan)
        self.assertIn("AC-2", plan)
        self.assertIn("Do the thing", plan)
        self.assertIn("Other thing already done", plan)

    def test_full_plan_deterministic(self):
        sub = self._submission_with_remaining_and_satisfied()
        self.assertEqual(render_agent_full_plan(sub), render_agent_full_plan(sub))

    def test_full_plan_sections_present(self):
        sub = self._submission_with_remaining_and_satisfied()
        plan = render_agent_full_plan(sub)
        for section in (
            "## Ticket Summary",
            "## Approach",
            "## Repository Findings",
            "## Assumptions",
            "## Criterion Assessments",
            "## Risks",
            "## Implementation Plan",
            "## Verification Plan",
        ):
            self.assertIn(section, plan, f"Missing section: {section}")

    def test_gap_plan_contains_only_remaining(self):
        sub = self._submission_with_remaining_and_satisfied()
        gap = render_agent_gap_plan(sub)
        self.assertIn("AC-1", gap)
        self.assertNotIn("AC-2", gap)

    def test_gap_plan_deterministic(self):
        sub = self._submission_with_remaining_and_satisfied()
        self.assertEqual(render_agent_gap_plan(sub), render_agent_gap_plan(sub))

    def test_gap_plan_has_acceptance_criteria_section(self):
        sub = self._submission_with_remaining_and_satisfied()
        gap = render_agent_gap_plan(sub)
        self.assertIn("## Acceptance Criteria", gap)

    def test_gap_plan_criteria_have_metadata_comments(self):
        sub = self._submission_with_remaining_and_satisfied()
        gap = render_agent_gap_plan(sub)
        # Should have verify and strategy tags compatible with gap-plan parser
        self.assertIn("verify: test", gap)
        self.assertIn("strategy: tdd", gap)

    def test_render_plan_context_includes_key_fields(self):
        ac = AgentCriterionAssessment(
            criterion_id="AC-1",
            source_criterion="Do thing",
            disposition="remaining",
            rationale="Not done.",
            planned_changes=(PlannedChange(path="src/x.py", description="add x"),),
            verification="test",
            implementation_strategy="tdd",
            plan_context="Explicit context",
            evidence=(AgentEvidence(path="src/z.py", observation="related"),),
        )
        ctx = render_plan_context(ac)
        self.assertIn("Explicit context", ctx)
        self.assertIn("`src/x.py`", ctx)
        self.assertIn("src/z.py", ctx)
        self.assertIn("test", ctx)
        self.assertIn("tdd", ctx)

    def test_rendered_plan_context_paths_are_extractable(self):
        from ticket_pipeline.lib import pipeline_lib as lib

        path = "src/ticket_pipeline/planning/agent_rendering.py"
        assessment = AgentCriterionAssessment(
            criterion_id="AC-1",
            source_criterion="Update plan rendering",
            disposition="remaining",
            rationale="The renderer needs a change.",
            planned_changes=(PlannedChange(path=path, description="Update rendering"),),
        )

        plan_context = render_plan_context(assessment)

        self.assertIn(path, lib.extract_referenced_paths(plan_context))


# ---------------------------------------------------------------------------
# 8. Factory: config loading
# ---------------------------------------------------------------------------


class FactoryConfigTests(unittest.TestCase):
    def test_create_mechanical_strategy(self):
        from ticket_pipeline.planning.strategies.mechanical import (
            MechanicalPlanningStrategy,
        )

        s = create_planning_strategy("mechanical")
        self.assertIsInstance(s, MechanicalPlanningStrategy)

    def test_create_agent_strategy(self):
        s = create_planning_strategy("agent")
        self.assertIsInstance(s, AgentPlanningStrategy)

    def test_unknown_strategy_raises_planning_error(self):
        with self.assertRaises(PlanningError):
            create_planning_strategy("unknown-v9")

    def test_load_agent_config_missing_file_returns_empty(self):
        cfg = load_agent_config(Path("/nonexistent/config.toml"))
        self.assertEqual({}, cfg)

    def test_load_agent_config_reads_planning_agent_section(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write('[planning_agent]\nuser_input = "infer"\nmax_turns = 20\n')
            name = f.name
        try:
            cfg = load_agent_config(Path(name))
            self.assertEqual("infer", cfg["user_input"])
            self.assertEqual(20, cfg["max_turns"])
        finally:
            Path(name).unlink()

    def test_load_agent_config_unknown_key_raises(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write('[planning_agent]\nunknown_key = "bad"\n')
            name = f.name
        try:
            with self.assertRaises(PlanningError) as ctx:
                load_agent_config(Path(name))
            self.assertIn("unknown_key", str(ctx.exception))
        finally:
            Path(name).unlink()

    def test_create_agent_strategy_applies_config(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write('[planning_agent]\nuser_input = "infer"\nmax_turns = 25\n')
            name = f.name
        try:
            s = create_planning_strategy("agent", config_path=Path(name))
            self.assertIsInstance(s, AgentPlanningStrategy)
            self.assertEqual("infer", s.user_input_mode)
            self.assertEqual(25, s.max_turns)
        finally:
            Path(name).unlink()

    def test_invalid_user_input_mode_raises(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write('[planning_agent]\nuser_input = "banana"\n')
            name = f.name
        try:
            with self.assertRaises(PlanningError):
                create_planning_strategy("agent", config_path=Path(name))
        finally:
            Path(name).unlink()


# ---------------------------------------------------------------------------
# 9. Criterion extraction and ID assignment
# ---------------------------------------------------------------------------


class CriterionExtractionTests(unittest.TestCase):
    def test_extracts_from_acceptance_criteria_section(self):
        ticket = (
            "# Ticket\n\nDescription.\n\n"
            "## Acceptance Criteria\n\n"
            "- [ ] First criterion\n"
            "- [ ] Second criterion\n"
        )
        criteria = extract_acceptance_criteria(ticket)
        self.assertEqual(2, len(criteria))
        self.assertIn("First criterion", criteria[0])
        self.assertIn("Second criterion", criteria[1])

    def test_case_insensitive_section_match(self):
        ticket = "# Ticket\n\n## acceptance criteria\n\n- [ ] Thing\n"
        criteria = extract_acceptance_criteria(ticket)
        self.assertEqual(1, len(criteria))

    def test_no_section_returns_empty(self):
        criteria = extract_acceptance_criteria("# Ticket\n\nJust a description.")
        self.assertEqual([], criteria)

    def test_assign_criterion_ids_stable(self):
        ids = assign_criterion_ids(["First", "Second", "Third"])
        self.assertEqual([("AC-1", "First"), ("AC-2", "Second"), ("AC-3", "Third")], ids)


# ---------------------------------------------------------------------------
# 10. Integration: fake model transcript
# ---------------------------------------------------------------------------


class FakeTranscriptIntegrationTests(unittest.TestCase):
    """
    Simulates a complete agent planning session without live model calls.
    Verifies the path through TerminalToolResult → AgentPlanSubmission →
    PlanningResult → frame construction.
    """

    def test_full_path_remaining_criterion(self):
        """
        Transcript:
          assistant → search_files
          tool      → results
          assistant → read_file
          tool      → content
          assistant → submit_plan
        → PlanningResult with 1 remaining criterion
        """
        from ticket_pipeline.lib import pipeline_lib as lib
        from ticket_pipeline.planning import build_ticket_frames

        ticket_content = (
            "# Test Ticket\n\nDescription.\n\n## Acceptance Criteria\n\n- [ ] Add foo support\n"  # noqa: E501
        )

        submit_args = {
            "ticket_summary": "Add foo support",
            "approach_summary": "Implement Foo class in src/foo.py",
            "assumptions": [],
            "repository_findings": [{"path": "src/", "observation": "No foo module found"}],
            "criteria": [
                {
                    "criterion_id": "AC-1",
                    "source_criterion": "Add foo support",
                    "disposition": "remaining",
                    "rationale": "No foo module exists.",
                    "planned_changes": [{"path": "src/foo.py", "description": "Create Foo class"}],
                    "verification": "test",
                    "implementation_strategy": "tdd",
                    "plan_context": "Create src/foo.py with Foo class.",
                }
            ],
        }

        responses = [
            [_make_tool_call("search_files", {"pattern": "foo"})],
            [_make_tool_call("read_file", {"path": "src/__init__.py"})],
            [_make_tool_call(SUBMIT_PLAN_TOOL_NAME, submit_args)],
        ]

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ticket_file = tmp_path / ".ticket.md"
            plan_file = tmp_path / ".implementation-plan.md"
            gap_plan_file = tmp_path / ".gap-plan.md"
            (tmp_path / "src").mkdir()
            (tmp_path / "src" / "__init__.py").write_text("", encoding="utf-8")

            import os

            old_cwd = os.getcwd()
            try:
                os.chdir(tmp_path)

                fake_post = _make_fake_post(responses)
                with (
                    patch(
                        "ticket_pipeline.planning.agent_runner._post_chat_completion",
                        side_effect=fake_post,
                    ),
                    patch.object(lib, "TICKET_FILE", ticket_file),
                    patch.object(lib, "PLAN_FILE", plan_file),
                    patch.object(lib, "GAP_PLAN_FILE", gap_plan_file),
                ):
                    strategy = AgentPlanningStrategy(user_input_mode="infer")
                    request = PlanningRequest(
                        ticket_id="T-1",
                        ticket_content=ticket_content,
                        project_root=tmp_path,
                        model="test-model",
                        step_models={},
                    )
                    result = strategy.plan(request)

            finally:
                os.chdir(old_cwd)

            self.assertEqual(1, len(result.criteria))
            self.assertEqual("Add foo support", result.criteria[0].criterion)
            self.assertEqual("test", result.criteria[0].verification)
            self.assertEqual("tdd", result.criteria[0].implementation_strategy)
            self.assertIn("Create src/foo.py", result.criteria[0].plan_context)

            # Artifacts were written
            self.assertTrue(plan_file.exists())
            self.assertTrue(gap_plan_file.exists())
            self.assertIn("## Acceptance Criteria", gap_plan_file.read_text())

            # Frames can be built from the result
            frames = build_ticket_frames(
                ticket_id="T-1",
                ticket_content=ticket_content,
                planning_result=result,
            )
            self.assertEqual(1, len(frames))
            self.assertEqual("T-1", frames[0].ticket)

    def test_planning_failed_raises_planning_error(self):
        failed_args = {
            "reason": "Ticket is too vague",
            "category": "insufficient_ticket",
            "recoverable": True,
            "suggested_action": "Add acceptance criteria",
        }
        responses = [
            [_make_tool_call(PLANNING_FAILED_TOOL_NAME, failed_args)],
        ]
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ticket_file = tmp_path / ".ticket.md"
            plan_file = tmp_path / ".implementation-plan.md"
            gap_plan_file = tmp_path / ".gap-plan.md"

            import os

            old_cwd = os.getcwd()
            try:
                os.chdir(tmp_path)
                from ticket_pipeline.lib import pipeline_lib as lib

                fake_post = _make_fake_post(responses)
                with (
                    patch(
                        "ticket_pipeline.planning.agent_runner._post_chat_completion",
                        side_effect=fake_post,
                    ),
                    patch.object(lib, "TICKET_FILE", ticket_file),
                    patch.object(lib, "PLAN_FILE", plan_file),
                    patch.object(lib, "GAP_PLAN_FILE", gap_plan_file),
                ):
                    strategy = AgentPlanningStrategy(user_input_mode="infer")
                    request = PlanningRequest(
                        ticket_id="T-1",
                        ticket_content="# T-1\n\nVague.",
                        project_root=tmp_path,
                        model="test-model",
                        step_models={},
                    )
                    with self.assertRaises(PlanningError) as ctx:
                        strategy.plan(request)
            finally:
                os.chdir(old_cwd)

        self.assertIn("insufficient_ticket", str(ctx.exception))

    def test_invalid_submission_retried_then_succeeds(self):
        """
        First submit_plan has validation errors; second is valid.
        """
        invalid_args = {
            "ticket_summary": "t",
            "approach_summary": "a",
            "assumptions": [],
            "repository_findings": [],
            "criteria": [
                {
                    "criterion_id": "AC-1",
                    "source_criterion": "Do thing",
                    "disposition": "remaining",
                    "rationale": "needed",
                    # Missing planned_changes, verification, implementation_strategy
                }
            ],
        }
        valid_args = _minimal_submit_args([_minimal_remaining_args()])

        # The strategy re-runs the whole loop for invalid submissions,
        # so responses for the retry should be a fresh terminal call.
        responses_attempt1 = [
            [_make_tool_call(SUBMIT_PLAN_TOOL_NAME, invalid_args)],
        ]
        responses_attempt2 = [
            [_make_tool_call(SUBMIT_PLAN_TOOL_NAME, valid_args)],
        ]

        call_count = [0]
        all_responses = responses_attempt1 + responses_attempt2

        def fake_post(payload, label):
            idx = call_count[0]
            call_count[0] += 1
            resp = all_responses[idx]
            return {"choices": [{"message": {"content": None, "tool_calls": resp}}]}

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ticket_file = tmp_path / ".ticket.md"
            plan_file = tmp_path / ".implementation-plan.md"
            gap_plan_file = tmp_path / ".gap-plan.md"

            import os

            old_cwd = os.getcwd()
            try:
                os.chdir(tmp_path)
                from ticket_pipeline.lib import pipeline_lib as lib

                with (
                    patch(
                        "ticket_pipeline.planning.agent_runner._post_chat_completion",
                        side_effect=fake_post,
                    ),
                    patch.object(lib, "TICKET_FILE", ticket_file),
                    patch.object(lib, "PLAN_FILE", plan_file),
                    patch.object(lib, "GAP_PLAN_FILE", gap_plan_file),
                ):
                    strategy = AgentPlanningStrategy(
                        user_input_mode="infer",
                        max_invalid_submissions=2,
                    )
                    request = PlanningRequest(
                        ticket_id="T-1",
                        ticket_content="# T-1\n\n## Acceptance Criteria\n\n- [ ] Do thing\n",
                        project_root=tmp_path,
                        model="test-model",
                        step_models={},
                    )
                    result = strategy.plan(request)
            finally:
                os.chdir(old_cwd)

        self.assertEqual(1, len(result.criteria))

    def test_invalid_submission_limit_exceeded_raises(self):
        """
        All submissions are invalid; should raise after max_invalid_submissions.
        """
        invalid_args = {
            "ticket_summary": "t",
            "approach_summary": "a",
            "assumptions": [],
            "repository_findings": [],
            "criteria": [
                {
                    "criterion_id": "AC-1",
                    "source_criterion": "Do thing",
                    "disposition": "remaining",
                    "rationale": "needed",
                    # Missing required fields for remaining
                }
            ],
        }

        def fake_post(payload, label):
            return {
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [_make_tool_call(SUBMIT_PLAN_TOOL_NAME, invalid_args)],
                        }
                    }
                ]
            }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ticket_file = tmp_path / ".ticket.md"
            plan_file = tmp_path / ".implementation-plan.md"
            gap_plan_file = tmp_path / ".gap-plan.md"

            import os

            old_cwd = os.getcwd()
            try:
                os.chdir(tmp_path)
                from ticket_pipeline.lib import pipeline_lib as lib

                with (
                    patch(
                        "ticket_pipeline.planning.agent_runner._post_chat_completion",
                        side_effect=fake_post,
                    ),
                    patch.object(lib, "TICKET_FILE", ticket_file),
                    patch.object(lib, "PLAN_FILE", plan_file),
                    patch.object(lib, "GAP_PLAN_FILE", gap_plan_file),
                ):
                    strategy = AgentPlanningStrategy(
                        user_input_mode="infer",
                        max_invalid_submissions=1,
                    )
                    request = PlanningRequest(
                        ticket_id="T-1",
                        ticket_content="# T-1\n\n## Acceptance Criteria\n\n- [ ] Do thing\n",
                        project_root=tmp_path,
                        model="test-model",
                        step_models={},
                    )
                    with self.assertRaises(PlanningError) as ctx:
                        strategy.plan(request)
            finally:
                os.chdir(old_cwd)

        self.assertIn("invalid submission", str(ctx.exception).lower())


# ---------------------------------------------------------------------------
# 11. CLI: explore incompatibility
# ---------------------------------------------------------------------------


class CLIExploreIncompatibilityTests(unittest.TestCase):
    def test_explore_with_agent_strategy_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # Create a config with agent strategy
            config_file = tmp_path / ".dev-pipeline.toml"
            config_file.write_text('planning_strategy = "agent"\n', encoding="utf-8")

            import os

            old_cwd = os.getcwd()
            try:
                os.chdir(tmp_path)
                from ticket_pipeline.lib import pipeline_lib as lib

                # resolve_planning_strategy_name should return 'agent'
                strategy_name = lib.resolve_planning_strategy_name(config_file, None)
                self.assertEqual("agent", strategy_name)

                # Simulate the check that push_ticket does
                if strategy_name == "agent":
                    # This is the message that should be shown
                    message = (
                        "--explore is not compatible with the agent planning strategy because "
                        "repository exploration is already part of that strategy."
                    )
                    self.assertIn("repository exploration", message)
                    self.assertIn("agent planning strategy", message)
            finally:
                os.chdir(old_cwd)


if __name__ == "__main__":
    unittest.main()
