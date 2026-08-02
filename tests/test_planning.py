import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ticket_pipeline import push_ticket
from ticket_pipeline.lib import pipeline_lib as lib
from ticket_pipeline.planning import (
    PlannedCriterion,
    PlanningError,
    PlanningRequest,
    PlanningResult,
    build_ticket_frames,
    create_planning_strategy,
    parse_gap_plan,
)
from ticket_pipeline.planning.strategies.mechanical import MechanicalPlanningStrategy


GAP_PLAN = """\
<!-- narrowed by Narrower -->

## Implementation Plan

### Criterion 1
Update `src/example.py` and verify behavior.

### Criterion 2
Touch `tests/test_example.py` only.

## Acceptance Criteria

- [ ] Change behavior <!-- why: missing; verify: test; strategy: direct; existing_test: tests/test_example.py::test_old -->
- [ ] Refactor docs <!-- why: tidy; verify: manual -->
"""


class PlanningModelTests(unittest.TestCase):
    def test_planned_criterion_defaults(self):
        item = PlannedCriterion(criterion="- [ ] Do thing", plan_context="Context")
        self.assertEqual("test", item.verification)
        self.assertEqual("tdd", item.implementation_strategy)
        self.assertEqual((), item.existing_test_refs)

    def test_invalid_verification_rejected(self):
        with self.assertRaises(ValueError):
            PlannedCriterion(
                criterion="- [ ] Do thing",
                plan_context="Context",
                verification="integration-only",
            )

    def test_invalid_strategy_rejected(self):
        with self.assertRaises(ValueError):
            PlannedCriterion(
                criterion="- [ ] Do thing",
                plan_context="Context",
                implementation_strategy="autonomous",
            )


class GapPlanParsingTests(unittest.TestCase):
    def test_parse_gap_plan_extracts_structured_criteria(self):
        criteria = parse_gap_plan(GAP_PLAN)
        self.assertEqual(2, len(criteria))
        self.assertEqual("direct", criteria[0].implementation_strategy)
        self.assertEqual("test", criteria[0].verification)
        self.assertEqual(
            ("tests/test_example.py::test_old",),
            criteria[0].existing_test_refs,
        )
        self.assertEqual("manual", criteria[1].verification)
        self.assertEqual("manual", criteria[1].implementation_strategy)


class FrameFactoryTests(unittest.TestCase):
    def test_build_ticket_frames_copies_fields(self):
        result = PlanningResult(
            criteria=(
                PlannedCriterion(
                    criterion="- [ ] Do thing",
                    plan_context="Context",
                    verification="manual",
                    implementation_strategy="direct",
                    existing_test_refs=("a::b",),
                ),
            )
        )
        frames = build_ticket_frames(
            ticket_id="TEST-1",
            ticket_content="# Ticket",
            planning_result=result,
        )
        self.assertEqual(1, len(frames))
        frame = frames[0]
        self.assertEqual("TEST-1", frame.ticket)
        self.assertEqual("# Ticket", frame.ticket_snapshot)
        self.assertEqual("Context", frame.plan_context)
        self.assertEqual("manual", frame.verification)
        self.assertEqual("direct", frame.strategy)
        self.assertEqual(["a::b"], frame.existing_test_refs)
        self.assertEqual("pending", frame.status)
        self.assertEqual("ticket", frame.origin)
        self.assertIsNone(frame.test_files)
        self.assertIsNone(frame.test_names)

    def test_build_ticket_frames_applies_override(self):
        result = PlanningResult(
            criteria=(
                PlannedCriterion(
                    criterion="- [ ] Do thing",
                    plan_context="Context",
                    implementation_strategy="manual",
                ),
            )
        )
        frames = build_ticket_frames(
            ticket_id="TEST-1",
            ticket_content="# Ticket",
            planning_result=result,
            strategy_override="direct",
        )
        self.assertEqual("direct", frames[0].strategy)


class StrategySelectionTests(unittest.TestCase):
    def test_precedence_cli_over_project_over_user_over_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            project = tmp_path / "project.toml"
            user = tmp_path / "user.toml"
            project.write_text('planning_strategy = "agent"\n', encoding="utf-8")
            user.write_text('planning_strategy = "mechanical"\n', encoding="utf-8")
            self.assertEqual(
                "mechanical",
                lib.resolve_planning_strategy_name(project, "mechanical", user),
            )
            self.assertEqual(
                "agent",
                lib.resolve_planning_strategy_name(project, None, user),
            )
            project.unlink()
            self.assertEqual(
                "mechanical",
                lib.resolve_planning_strategy_name(project, None, user),
            )
            user.unlink()
            self.assertEqual(
                "mechanical",
                lib.resolve_planning_strategy_name(project, None, user),
            )

    def test_invalid_config_value_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text('planning_strategy = "autonomous-v2"\n', encoding="utf-8")
            with self.assertRaises(SystemExit):
                lib.resolve_planning_strategy_name(config, None, config)

    def test_agent_strategy_placeholder_fails_clearly(self):
        strategy = create_planning_strategy("agent")
        with self.assertRaises(PlanningError) as ctx:
            strategy.plan(
                PlanningRequest(
                    ticket_id="TEST-1",
                    ticket_content="# Ticket",
                    project_root=Path.cwd(),
                    model="model",
                    step_models={},
                )
            )
        self.assertIn("not implemented", str(ctx.exception))


class MechanicalStrategyTests(unittest.TestCase):
    def test_mechanical_strategy_returns_structured_result(self):
        strategy = MechanicalPlanningStrategy()
        request = PlanningRequest(
            ticket_id="TEST-1",
            ticket_content="# Ticket",
            project_root=Path.cwd(),
            model="model",
            step_models={"plan": "plan-model", "narrow": "narrow-model"},
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ticket_file = tmp_path / ".ticket.md"
            plan_file = tmp_path / ".tdd-plan.md"
            gap_plan_file = tmp_path / ".gap-plan.md"

            def fake_walk(_blocks):
                plan_file.write_text("## Acceptance Criteria\n\n- [ ] plan\n", encoding="utf-8")
                gap_plan_file.write_text(GAP_PLAN, encoding="utf-8")

            with (
                patch.object(lib, "TICKET_FILE", ticket_file),
                patch.object(lib, "PLAN_FILE", plan_file),
                patch.object(lib, "GAP_PLAN_FILE", gap_plan_file),
                patch.object(lib, "remove_scratch_files"),
                patch.object(lib, "walk", side_effect=fake_walk) as walk,
                patch.object(lib, "build_planning_blocks", return_value=["planner"]) as blocks,
            ):
                result = strategy.plan(request)

            self.assertEqual("# Ticket", ticket_file.read_text(encoding="utf-8"))
            blocks.assert_called_once_with(
                ticket_id="TEST-1",
                model="model",
                step_models={"plan": "plan-model", "narrow": "narrow-model"},
                ticket_file_in=ticket_file,
            )
            walk.assert_called_once_with(["planner"])
            self.assertEqual(2, len(result.criteria))
            self.assertIn("## Acceptance Criteria", result.plan_text or "")
            self.assertEqual(GAP_PLAN, result.narrowed_plan_text)


class ResolveTicketFramesIntegrationTests(unittest.TestCase):
    def test_fake_strategy_can_seed_frames_without_gap_plan_artifact(self):
        class FakeStrategy:
            def plan(self, request):
                return PlanningResult(
                    criteria=(
                        PlannedCriterion(
                            criterion="- [ ] Fake criterion",
                            plan_context="Context",
                        ),
                    )
                )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            gap_plan_file = tmp_path / ".gap-plan.md"
            with (
                patch.object(push_ticket, "load_ticket_content", return_value="# Ticket"),
                patch.object(push_ticket, "create_planning_strategy", return_value=FakeStrategy()),
                patch.object(lib, "filter_grounded_frames", side_effect=lambda frames: (frames, [], 0)),
                patch.object(lib, "GAP_PLAN_FILE", gap_plan_file),
            ):
                frames = push_ticket.resolve_ticket_frames(
                    ticket_id="TEST-1",
                    model="model",
                    step_models={},
                    ticket_file_in=None,
                    planning_strategy_name="mechanical",
                )
        self.assertEqual(1, len(frames))
        self.assertFalse(gap_plan_file.exists())
        self.assertEqual("# Ticket", frames[0].ticket_snapshot)
