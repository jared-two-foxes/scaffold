"""
Tests for ticket snapshot persistence across the criteria-stack pipeline:

1. push_ticket.resolve_ticket_frames stores the fetched ticket text in each
   frame's ticket_snapshot field.
2. ensure_validating_sentinel stores ticket_snapshot in the sentinel frame.
3. CriterionFrame round-trips ticket_snapshot through save_stack/load_stack.
4. Older stack files without the ticket_snapshot key deserialise cleanly
   (backward compatibility - ticket_snapshot defaults to None).
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ticket_pipeline.lib import pipeline_lib as lib


SAMPLE_TICKET = "# TEST-1 — Sample ticket\n\n## Description\n\nDo something.\n"

GAP_PLAN = """\
<!-- narrowed by Narrower -->

## Implementation Plan

### Criterion 1
Do the thing.

## Acceptance Criteria

- [ ] The thing is done <!-- why: not done yet; verify: test -->
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
            gap_plan_file = tmp_path / ".gap-plan.md"
            gap_plan_file.write_text(GAP_PLAN, encoding="utf-8")

            with (
                patch.object(lib, "fetch_ticket_text", return_value=ticket_content),
                patch.object(lib, "remove_scratch_files"),
                patch.object(lib, "TICKET_FILE", ticket_file),
                patch.object(lib, "GAP_PLAN_FILE", gap_plan_file),
                patch.object(lib, "walk"),
                patch.object(lib, "filter_grounded_frames") as mock_filter,
            ):
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
            frames = self._run_resolve("TEST-1", ticket_file_in=ticket_file_in, ticket_content=SAMPLE_TICKET)

        self.assertTrue(len(frames) > 0, "Expected at least one frame")
        for frame in frames:
            self.assertEqual(frame.ticket_snapshot, SAMPLE_TICKET)
