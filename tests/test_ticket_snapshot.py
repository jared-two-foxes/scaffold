"""
Tests for ticket snapshot persistence across the criteria-stack pipeline:

1. push_ticket.resolve_ticket_frames stores the fetched ticket text in each
   frame's ticket_snapshot field.
2. ensure_validating_sentinel stores ticket_snapshot in the sentinel frame.
3. CriterionFrame round-trips ticket_snapshot through save_stack/load_stack.
4. Older stack files without the ticket_snapshot key deserialise cleanly
   (backward compatibility - ticket_snapshot defaults to None).
5. A validate-missed ticket validation cycle preserves the original ticket
   snapshot through the pop/resume path without re-fetching from Linear.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ticket_pipeline import next_step, push_ticket
from ticket_pipeline.lib import pipeline_lib as lib
from ticket_pipeline.planning.models import PlannedCriterion, PlanningResult

SAMPLE_TICKET = "# TEST-1 — Sample ticket\n\n## Description\n\nDo something.\n"

GAP_PLAN = """\
<!-- narrowed by Narrower -->

## Implementation Plan

### Criterion 1
Do the thing.

## Acceptance Criteria

- [ ] The thing is done <!-- why: not done yet; verify: test; strategy: direct -->
"""


class TestTicketSnapshotOnFrame(unittest.TestCase):
    """ticket_snapshot is preserved through CriterionFrame serialisation."""

    def test_snapshot_round_trips_through_save_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            stack_file = Path(tmp) / ".criteria-stack.json"
            with patch.object(lib, "CRITERIA_STACK_FILE", stack_file):
                frame = lib.CriterionFrame(
                    ticket="TEST-1",
                    criterion="- [ ] The thing is done",
                    plan_context="Do the thing.",
                    test_files=None,
                    test_names=None,
                    status="pending",
                    origin="ticket",
                    ticket_snapshot=SAMPLE_TICKET,
                )
                lib.save_stack([frame])
                loaded = lib.load_stack()
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].ticket_snapshot, SAMPLE_TICKET)

    def test_snapshot_none_when_absent_from_old_json(self):
        """Stack files written before ticket_snapshot existed load cleanly."""
        with tempfile.TemporaryDirectory() as tmp:
            stack_file = Path(tmp) / ".criteria-stack.json"
            # Write a JSON entry without the ticket_snapshot key.
            old_entry = {
                "ticket": "TEST-1",
                "criterion": "- [ ] The thing is done",
                "plan_context": "Do the thing.",
                "test_files": None,
                "test_names": None,
                "status": "pending",
                "origin": "ticket",
            }
            stack_file.write_text(json.dumps([old_entry]) + "\n", encoding="utf-8")
            with patch.object(lib, "CRITERIA_STACK_FILE", stack_file):
                loaded = lib.load_stack()
            self.assertEqual(len(loaded), 1)
            self.assertIsNone(loaded[0].ticket_snapshot)


class TestEnsureValidatingSentinel(unittest.TestCase):
    """ensure_validating_sentinel stores ticket_snapshot in the new sentinel."""

    def test_sentinel_carries_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            stack_file = Path(tmp) / ".criteria-stack.json"
            with patch.object(lib, "CRITERIA_STACK_FILE", stack_file):
                lib.ensure_validating_sentinel("TEST-1", ticket_snapshot=SAMPLE_TICKET)
                stack = lib.load_stack()
            self.assertEqual(len(stack), 1)
            sentinel = stack[0]
            self.assertEqual(sentinel.status, lib.VALIDATING_STATUS)
            self.assertEqual(sentinel.ticket_snapshot, SAMPLE_TICKET)

    def test_sentinel_snapshot_none_when_not_provided(self):
        with tempfile.TemporaryDirectory() as tmp:
            stack_file = Path(tmp) / ".criteria-stack.json"
            with patch.object(lib, "CRITERIA_STACK_FILE", stack_file):
                lib.ensure_validating_sentinel("TEST-1")
                stack = lib.load_stack()
            self.assertIsNone(stack[0].ticket_snapshot)

    def test_sentinel_idempotent_does_not_overwrite(self):
        """A second call does nothing even if a different snapshot is given."""
        with tempfile.TemporaryDirectory() as tmp:
            stack_file = Path(tmp) / ".criteria-stack.json"
            with patch.object(lib, "CRITERIA_STACK_FILE", stack_file):
                lib.ensure_validating_sentinel("TEST-1", ticket_snapshot=SAMPLE_TICKET)
                lib.ensure_validating_sentinel("TEST-1", ticket_snapshot="different")
                stack = lib.load_stack()
            self.assertEqual(len(stack), 1)
            # First call wins; the second call is a no-op.
            self.assertEqual(stack[0].ticket_snapshot, SAMPLE_TICKET)


class TestResolveTicketFramesSnapshot(unittest.TestCase):
    """push_ticket.resolve_ticket_frames populates ticket_snapshot on every frame."""

    def _run_resolve(self, ticket_id, ticket_file_in, ticket_content):
        """Helper: run resolve_ticket_frames with filesystem isolated to tmp."""
        from ticket_pipeline import push_ticket

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ticket_file = tmp_path / ".ticket.md"
            plan_file = tmp_path / ".implementation-plan.md"
            gap_plan_file = tmp_path / ".gap-plan.md"
            gap_plan_file.write_text(GAP_PLAN, encoding="utf-8")

            with (
                patch.object(lib, "fetch_ticket_text", return_value=ticket_content),
                patch.object(lib, "remove_scratch_files"),
                patch.object(lib, "TICKET_FILE", ticket_file),
                patch.object(lib, "PLAN_FILE", plan_file),
                patch.object(lib, "GAP_PLAN_FILE", gap_plan_file),
                patch.object(lib, "walk") as mock_walk,
                patch.object(lib, "filter_grounded_frames") as mock_filter,
            ):

                def fake_walk(_blocks):
                    plan_file.write_text(
                        "## Acceptance Criteria\n\n- [ ] The thing is done\n",
                        encoding="utf-8",
                    )

                mock_walk.side_effect = fake_walk

                def passthrough(candidate_frames):
                    return candidate_frames, [], 0

                mock_filter.side_effect = passthrough

                return push_ticket.resolve_ticket_frames(
                    ticket_id=ticket_id,
                    model="some-model",
                    step_models={},
                    ticket_file_in=ticket_file_in,
                )

    def test_snapshot_set_on_frames_when_fetching_from_linear(self):
        frames = self._run_resolve("TEST-1", ticket_file_in=None, ticket_content=SAMPLE_TICKET)
        self.assertTrue(len(frames) > 0, "Expected at least one frame")
        for frame in frames:
            self.assertEqual(
                frame.ticket_snapshot,
                SAMPLE_TICKET,
                f"Frame '{frame.criterion}' is missing ticket_snapshot",
            )

    def test_snapshot_set_on_frames_when_reading_from_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            ticket_file_in = Path(tmp) / "local_ticket.md"
            ticket_file_in.write_text(SAMPLE_TICKET, encoding="utf-8")
            frames = self._run_resolve(
                "TEST-1",
                ticket_file_in=ticket_file_in,
                ticket_content=SAMPLE_TICKET,
            )

        self.assertTrue(len(frames) > 0, "Expected at least one frame")
        for frame in frames:
            self.assertEqual(frame.ticket_snapshot, SAMPLE_TICKET)


class TestReviewFindingsSnapshot(unittest.TestCase):
    def test_review_findings_carry_ticket_snapshot(self):
        review_text = "CHANGES REQUESTED\n- fix the bug"
        captured: dict[str, object] = {}

        def fake_filter(frames):
            captured["frames"] = frames
            return frames, [], 0

        with (
            patch.object(lib, "filter_grounded_frames", side_effect=fake_filter),
            patch.object(lib, "push_frames") as mock_push,
            patch.object(next_step.sys, "exit") as mock_exit,
        ):
            next_step.do_push_review_findings(
                "TEST-1",
                review_text,
                ticket_content=SAMPLE_TICKET,
            )

        frames = captured["frames"]
        self.assertEqual(1, len(frames))
        self.assertEqual(SAMPLE_TICKET, frames[0].ticket_snapshot)
        mock_push.assert_called_once()
        mock_exit.assert_called_once_with(0)


class TestValidateThreadsTicketSnapshot(unittest.TestCase):
    def test_validate_passes_snapshot_to_review_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            stack_file = Path(tmp) / ".criteria-stack.json"
            ticket_file = Path(tmp) / ".ticket.md"
            with (
                patch.object(lib, "CRITERIA_STACK_FILE", stack_file),
                patch.object(lib, "TICKET_FILE", ticket_file),
                patch.object(lib, "ensure_validating_sentinel") as mock_sentinel,
                patch.object(lib, "fetch_ticket_text") as mock_fetch,
                patch.object(lib, "_resolve_plan_file", return_value=None),
                patch.object(lib, "run_plan_step", return_value="## Implementation Plan\n"),
                patch.object(lib, "run_narrow_step", return_value="## Acceptance Criteria\n"),
                patch.object(lib, "extract_acceptance_criteria", return_value=[]),
                patch.object(lib, "run_lint_gate"),
                patch.object(
                    lib, "run_command", return_value=type("Result", (), {"returncode": 0})()
                ),
                patch.object(lib, "load_smoke_cmd", return_value=None),
                patch.object(lib, "run_smoke_gate"),
                patch.object(lib, "git_changed_files", return_value=["src/example.py"]),
                patch.object(lib, "run_review_gate", return_value=("CHANGES REQUESTED", "review")),
                patch.object(next_step, "do_push_review_findings") as mock_review,
            ):
                next_step.do_ticket_validate(
                    "TEST-1",
                    next_step.lib.StepContext(
                        model="model",
                        step_models={},
                        commands={"test_cmd": "true"},
                        config_path=Path(".dev-pipeline.toml"),
                        continuous=False,
                        max_attempts=3,
                        retry_policy=None,
                        accept_green=False,
                        accept_manual=False,
                        accept_no_test=False,
                        skip_implementation=False,
                        allow_compile=True,
                        reset_on_retry=True,
                        git_cfg=None,
                    ),
                    ticket_snapshot=SAMPLE_TICKET,
                )

        mock_sentinel.assert_called_once_with("TEST-1", ticket_snapshot=SAMPLE_TICKET)
        mock_fetch.assert_not_called()
        mock_review.assert_called_once_with("TEST-1", "review", ticket_content=SAMPLE_TICKET)


class TestValidateMissedSnapshotCarryforward(unittest.TestCase):
    def test_validate_missed_cycle_reuses_snapshot_without_fetch(self):
        class FakePlanningStrategy:
            def plan(self, request):
                return PlanningResult(
                    criteria=(
                        PlannedCriterion(
                            criterion="- [ ] Popped follow-up criterion",
                            plan_context="Context",
                            verification="test",
                            implementation_strategy="tdd",
                        ),
                    )
                )

        with tempfile.TemporaryDirectory() as tmp:
            stack_file = Path(tmp) / ".criteria-stack.json"
            ticket_file_in = Path(tmp) / "seeded_ticket.md"
            ticket_file = Path(tmp) / ".ticket.md"
            ticket_file_in.write_text(SAMPLE_TICKET, encoding="utf-8")
            ctx = lib.StepContext(
                model="model",
                step_models={},
                commands={"test_cmd": "true"},
                config_path=Path(".dev-pipeline.toml"),
                continuous=False,
                max_attempts=3,
                retry_policy=None,
                accept_green=False,
                accept_manual=False,
                accept_no_test=False,
                skip_implementation=False,
                allow_compile=True,
                reset_on_retry=True,
                git_cfg=None,
            )

            with (
                patch.object(lib, "CRITERIA_STACK_FILE", stack_file),
                patch.object(lib, "TICKET_FILE", ticket_file),
                patch.object(lib, "fetch_ticket_text") as mock_fetch,
                patch.object(
                    push_ticket, "create_planning_strategy", return_value=FakePlanningStrategy()
                ),
                patch.object(
                    lib, "filter_grounded_frames", side_effect=lambda frames: (frames, [], 0)
                ),
                patch.object(lib, "_resolve_plan_file", return_value=None),
                patch.object(lib, "run_plan_step", return_value="## Implementation Plan\n"),
                patch.object(
                    lib,
                    "run_narrow_step",
                    side_effect=[
                        "## Acceptance Criteria\n- [ ] validate-missed criterion\n",
                        "## Acceptance Criteria\n",
                    ],
                ),
                patch.object(
                    lib,
                    "extract_acceptance_criteria",
                    side_effect=[["- [ ] validate-missed criterion"], []],
                ),
                patch.object(lib, "run_lint_gate"),
                patch.object(
                    lib, "run_command", return_value=type("Result", (), {"returncode": 0})()
                ),
                patch.object(lib, "load_smoke_cmd", return_value=None),
                patch.object(lib, "run_smoke_gate"),
                patch.object(lib, "git_changed_files", return_value=["src/example.py"]),
                patch.object(lib, "run_review_gate", return_value=("APPROVED", "review")),
                patch.object(next_step.sys, "exit") as mock_exit,
            ):
                seeded_frames = push_ticket.resolve_ticket_frames(
                    ticket_id="TEST-1",
                    model="model",
                    step_models={},
                    ticket_file_in=ticket_file_in,
                    planning_strategy_name="mechanical",
                )
                self.assertEqual(1, len(seeded_frames))
                self.assertEqual(SAMPLE_TICKET, seeded_frames[0].ticket_snapshot)

                next_step.do_ticket_validate(
                    "TEST-1", ctx, ticket_snapshot=seeded_frames[0].ticket_snapshot
                )
                stack = lib.load_stack()
                self.assertEqual(2, len(stack))
                self.assertEqual("validate-missed", stack[0].origin)
                self.assertEqual(SAMPLE_TICKET, stack[0].ticket_snapshot)
                self.assertEqual(lib.VALIDATING_STATUS, stack[1].status)
                self.assertEqual(SAMPLE_TICKET, stack[1].ticket_snapshot)

                next_step.do_pop(stack[0], ctx)
                stack = lib.load_stack()
                self.assertEqual(1, len(stack))
                self.assertEqual(lib.VALIDATING_STATUS, stack[0].status)
                self.assertEqual(SAMPLE_TICKET, stack[0].ticket_snapshot)

                next_step.step("model", {"test_cmd": "true"}, False, Path(".dev-pipeline.toml"))

            mock_fetch.assert_not_called()
            self.assertGreaterEqual(mock_exit.call_count, 2)


class TestValidateMissedSnapshotNoFetch(unittest.TestCase):
    def test_validate_missed_cycle_from_ticket_file_in_does_not_fetch_ticket_text(self):
        class FakePlanningStrategy:
            def plan(self, request):
                return PlanningResult(
                    criteria=(
                        PlannedCriterion(
                            criterion="- [ ] Popped follow-up criterion",
                            plan_context="Context",
                            verification="test",
                            implementation_strategy="tdd",
                        ),
                    )
                )

        ticket_file_in = Path(".test-seeded-ticket.md")
        stack_file = Path(".test-criteria-stack.json")
        ticket_file_in.write_text(SAMPLE_TICKET, encoding="utf-8")
        self.addCleanup(lambda: ticket_file_in.unlink(missing_ok=True))
        self.addCleanup(lambda: stack_file.unlink(missing_ok=True))

        stack_state: list[lib.CriterionFrame] = []

        def fake_load_stack():
            return list(stack_state)

        def fake_save_stack(frames):
            stack_state[:] = list(frames)

        ctx = lib.StepContext(
            model="model",
            step_models={},
            commands={"test_cmd": "true"},
            config_path=Path(".dev-pipeline.toml"),
            continuous=False,
            max_attempts=3,
            retry_policy=None,
            accept_green=False,
            accept_manual=False,
            accept_no_test=False,
            skip_implementation=False,
            allow_compile=True,
            reset_on_retry=True,
            git_cfg=None,
        )

        with (
            patch.object(lib, "CRITERIA_STACK_FILE", stack_file),
            patch.object(lib, "load_stack", side_effect=fake_load_stack),
            patch.object(lib, "save_stack", side_effect=fake_save_stack),
            patch.object(
                next_step.tools,
                "write_file_block",
                side_effect=lambda path: (lambda content: content),
            ),
            patch.object(
                lib,
                "fetch_ticket_text",
                side_effect=AssertionError("fetch_ticket_text should not be called"),
            ) as mock_fetch,
            patch.object(
                push_ticket, "create_planning_strategy", return_value=FakePlanningStrategy()
            ),
            patch.object(lib, "filter_grounded_frames", side_effect=lambda frames: (frames, [], 0)),
            patch.object(lib, "_resolve_plan_file", return_value=None),
            patch.object(lib, "run_plan_step", return_value="## Implementation Plan\n"),
            patch.object(
                lib,
                "run_narrow_step",
                side_effect=[
                    "## Acceptance Criteria\n- [ ] validate-missed criterion\n",
                    "## Acceptance Criteria\n",
                ],
            ),
            patch.object(
                lib,
                "extract_acceptance_criteria",
                side_effect=[["- [ ] validate-missed criterion"], []],
            ),
            patch.object(lib, "run_lint_gate"),
            patch.object(lib, "run_command", return_value=type("Result", (), {"returncode": 0})()),
            patch.object(lib, "load_smoke_cmd", return_value=None),
            patch.object(lib, "run_smoke_gate"),
            patch.object(lib, "git_changed_files", return_value=["src/example.py"]),
            patch.object(lib, "run_review_gate", return_value=("APPROVED", "review")),
            patch.object(next_step.sys, "exit") as mock_exit,
        ):
            seeded_frames = push_ticket.resolve_ticket_frames(
                ticket_id="TEST-1",
                model="model",
                step_models={},
                ticket_file_in=ticket_file_in,
                planning_strategy_name="mechanical",
            )
            self.assertEqual(1, len(seeded_frames))
            self.assertEqual(SAMPLE_TICKET, seeded_frames[0].ticket_snapshot)

            next_step.do_ticket_validate(
                "TEST-1", ctx, ticket_snapshot=seeded_frames[0].ticket_snapshot
            )
            stack = lib.load_stack()
            self.assertEqual(2, len(stack))
            self.assertEqual("validate-missed", stack[0].origin)
            self.assertEqual(SAMPLE_TICKET, stack[0].ticket_snapshot)
            self.assertEqual(lib.VALIDATING_STATUS, stack[1].status)
            self.assertEqual(SAMPLE_TICKET, stack[1].ticket_snapshot)

            next_step.do_pop(stack[0], ctx)
            stack = lib.load_stack()
            self.assertEqual(1, len(stack))
            self.assertEqual(lib.VALIDATING_STATUS, stack[0].status)
            self.assertEqual(SAMPLE_TICKET, stack[0].ticket_snapshot)

            next_step.step("model", {"test_cmd": "true"}, False, Path(".dev-pipeline.toml"))

        mock_fetch.assert_not_called()
        self.assertGreaterEqual(mock_exit.call_count, 2)


class TestValidateOnlyAndFromGapPlanWithoutSnapshots(unittest.TestCase):
    def _make_ctx(self) -> lib.StepContext:
        return lib.StepContext(
            model="model",
            step_models={},
            commands={"test_cmd": "true"},
            config_path=Path(".dev-pipeline.toml"),
            continuous=False,
            max_attempts=3,
            retry_policy=None,
            accept_green=False,
            accept_manual=False,
            accept_no_test=False,
            skip_implementation=False,
            allow_compile=True,
            reset_on_retry=True,
            git_cfg=None,
        )

    def test_validate_only_pushes_sentinel_without_snapshot_and_fetches_on_validate(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            stack_file = tmp_path / ".criteria-stack.json"
            ticket_file = tmp_path / ".ticket.md"
            plan_file = tmp_path / ".implementation-plan.md"
            gap_plan_file = tmp_path / ".gap-plan.md"

            with (
                patch.object(lib, "CRITERIA_STACK_FILE", stack_file),
                patch.object(lib, "TICKET_FILE", ticket_file),
                patch.object(lib, "PLAN_FILE", plan_file),
                patch.object(lib, "GAP_PLAN_FILE", gap_plan_file),
                patch.object(lib, "resolve_step_models", return_value=("model", {})),
                patch.object(
                    lib,
                    "load_git_config",
                    return_value=type("GitConfig", (), {"git_workflow": False})(),
                ),
                patch.object(
                    push_ticket.sys,
                    "argv",
                    [
                        "push-ticket",
                        "TEST-1",
                        "--validate-only",
                        "--log-level",
                        "warning",
                    ],
                ),
            ):
                push_ticket.main()

            stack = lib.load_stack()
            self.assertEqual(1, len(stack))
            self.assertEqual(lib.VALIDATING_STATUS, stack[0].status)
            self.assertIsNone(stack[0].ticket_snapshot)

            ctx = self._make_ctx()
            with (
                patch.object(lib, "CRITERIA_STACK_FILE", stack_file),
                patch.object(lib, "TICKET_FILE", ticket_file),
                patch.object(lib, "_resolve_plan_file", return_value=None),
                patch.object(lib, "fetch_ticket_text", return_value=SAMPLE_TICKET) as mock_fetch,
                patch.object(lib, "run_plan_step", return_value="## Implementation Plan\n"),
                patch.object(lib, "run_narrow_step", return_value="## Acceptance Criteria\n"),
                patch.object(lib, "extract_acceptance_criteria", return_value=[]),
                patch.object(lib, "run_lint_gate"),
                patch.object(
                    lib, "run_command", return_value=type("Result", (), {"returncode": 0})()
                ),
                patch.object(lib, "load_smoke_cmd", return_value=None),
                patch.object(lib, "run_smoke_gate"),
                patch.object(lib, "git_changed_files", return_value=["src/example.py"]),
                patch.object(lib, "run_review_gate", return_value=("APPROVED", "review")),
                patch.object(next_step.sys, "exit") as mock_exit,
            ):
                next_step.do_ticket_validate(
                    "TEST-1", ctx, ticket_snapshot=stack[0].ticket_snapshot
                )

            mock_fetch.assert_called_once_with("TEST-1")
            mock_exit.assert_called_once_with(0)

    def test_from_gap_plan_produces_frames_without_snapshot_and_validate_fetches(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            stack_file = tmp_path / ".criteria-stack.json"
            ticket_file = tmp_path / ".ticket.md"
            plan_file = tmp_path / ".implementation-plan.md"
            gap_plan_file = tmp_path / ".gap-plan.md"
            gap_plan_file.write_text(
                "## Acceptance Criteria\n\n- [ ] The thing is done\n",
                encoding="utf-8",
            )

            planning_result = PlanningResult(
                criteria=(
                    PlannedCriterion(
                        criterion="- [ ] The thing is done",
                        plan_context="Do the thing.",
                        verification="test",
                        implementation_strategy="tdd",
                    ),
                )
            )

            with (
                patch.object(lib, "CRITERIA_STACK_FILE", stack_file),
                patch.object(lib, "TICKET_FILE", ticket_file),
                patch.object(lib, "PLAN_FILE", plan_file),
                patch.object(lib, "GAP_PLAN_FILE", gap_plan_file),
                patch.object(lib, "resolve_step_models", return_value=("model", {})),
                patch.object(
                    lib,
                    "load_git_config",
                    return_value=type("GitConfig", (), {"git_workflow": False})(),
                ),
                patch.object(
                    push_ticket, "planning_result_from_gap_plan", return_value=planning_result
                ),
                patch.object(
                    lib, "filter_grounded_frames", side_effect=lambda frames: (frames, [], 0)
                ),
                patch.object(
                    push_ticket.sys,
                    "argv",
                    [
                        "push-ticket",
                        "TEST-1",
                        "--from-gap-plan",
                        "--log-level",
                        "warning",
                    ],
                ),
            ):
                push_ticket.main()

            frames = lib.load_stack()
            self.assertEqual(1, len(frames))
            self.assertEqual("ticket", frames[0].origin)
            self.assertIsNone(frames[0].ticket_snapshot)

            ctx = self._make_ctx()
            with (
                patch.object(lib, "CRITERIA_STACK_FILE", stack_file),
                patch.object(lib, "TICKET_FILE", ticket_file),
                patch.object(lib, "_resolve_plan_file", return_value=None),
                patch.object(lib, "fetch_ticket_text", return_value=SAMPLE_TICKET) as mock_fetch,
                patch.object(lib, "run_plan_step", return_value="## Implementation Plan\n"),
                patch.object(lib, "run_narrow_step", return_value="## Acceptance Criteria\n"),
                patch.object(lib, "extract_acceptance_criteria", return_value=[]),
                patch.object(lib, "run_lint_gate"),
                patch.object(
                    lib, "run_command", return_value=type("Result", (), {"returncode": 0})()
                ),
                patch.object(lib, "load_smoke_cmd", return_value=None),
                patch.object(lib, "run_smoke_gate"),
                patch.object(lib, "git_changed_files", return_value=["src/example.py"]),
                patch.object(lib, "run_review_gate", return_value=("APPROVED", "review")),
                patch.object(next_step.sys, "exit") as mock_exit,
            ):
                next_step.do_ticket_validate(
                    "TEST-1", ctx, ticket_snapshot=frames[0].ticket_snapshot
                )

            mock_fetch.assert_called_once_with("TEST-1")
            mock_exit.assert_called_once_with(0)
