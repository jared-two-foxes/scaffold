"""
Tests verifying that Scaffold's shared planning and execution infrastructure
does not impose TDD assumptions on non-TDD strategies, and that verification
and implementation strategy are treated as independent axes.

Covers the acceptance criteria from:
  scratch/scaffold-remove-implicit-tdd-assumptions-ticket.md
"""

import unittest
from unittest import mock

from ticket_pipeline import next_step
from ticket_pipeline.lib import pipeline_lib as lib
from ticket_pipeline.planning import (
    PlannedCriterion,
    PlanningError,
    parse_gap_plan,
)
from ticket_pipeline.strategies import refactor as refactor_strategy
from ticket_pipeline.strategies import tdd as tdd_strategy

# ---------------------------------------------------------------------------
# Model-level independence
# ---------------------------------------------------------------------------


class PlannedCriterionExplicitnessTests(unittest.TestCase):
    """PlannedCriterion must require explicit values for both axes."""

    def test_planned_criterion_requires_explicit_verification(self):
        """Missing verification must not silently default to 'test'."""
        with self.assertRaises((TypeError, ValueError)):
            PlannedCriterion(
                criterion="- [ ] Behavior change",
                plan_context="Context",
                implementation_strategy="direct",
                # verification deliberately omitted
            )

    def test_planned_criterion_requires_explicit_implementation_strategy(self):
        """Missing implementation_strategy must not silently default to 'tdd'."""
        with self.assertRaises((TypeError, ValueError)):
            PlannedCriterion(
                criterion="- [ ] Behavior change",
                plan_context="Context",
                verification="test",
                # implementation_strategy deliberately omitted
            )

    def test_test_verification_can_use_direct_implementation(self):
        """verify=test is independent of strategy=tdd; direct is equally valid."""
        item = PlannedCriterion(
            criterion="- [ ] Change observable behavior",
            plan_context="Context",
            verification="test",
            implementation_strategy="direct",
        )
        self.assertEqual("test", item.verification)
        self.assertEqual("direct", item.implementation_strategy)

    def test_direct_strategy_does_not_require_test_references(self):
        """A direct-strategy criterion needs no existing_test_refs."""
        item = PlannedCriterion(
            criterion="- [ ] Implement the feature",
            plan_context="Context",
            verification="test",
            implementation_strategy="direct",
        )
        self.assertEqual((), item.existing_test_refs)

    def test_tdd_strategy_still_constructs_correctly(self):
        """TDD remains a valid, fully supported strategy."""
        item = PlannedCriterion(
            criterion="- [ ] Red-green cycle needed",
            plan_context="Context",
            verification="test",
            implementation_strategy="tdd",
        )
        self.assertEqual("tdd", item.implementation_strategy)


# ---------------------------------------------------------------------------
# Parser-level independence
# ---------------------------------------------------------------------------


_GAP_PLAN_WITH_TAGS = (
    "## Acceptance Criteria\n\n"
    "- [ ] Feature A works <!-- why: not yet; verify: test; strategy: direct -->\n"
    "- [ ] Doc updated <!-- why: stale; verify: manual; strategy: direct -->\n"
)

_GAP_PLAN_MISSING_STRATEGY = (
    "## Acceptance Criteria\n\n- [ ] Feature A works <!-- why: not yet; verify: test -->\n"
)

_GAP_PLAN_MISSING_VERIFICATION = (
    "## Acceptance Criteria\n\n- [ ] Feature A works <!-- why: not yet; strategy: direct -->\n"
)

_GAP_PLAN_NO_TAGS = "## Acceptance Criteria\n\n- [ ] Feature A works <!-- why: not yet -->\n"


class ParserRejectsImplicitDefaultsTests(unittest.TestCase):
    """parse_gap_plan must reject criteria without explicit tags."""

    def test_parser_rejects_missing_strategy_instead_of_defaulting_to_tdd(self):
        with self.assertRaises(PlanningError) as ctx:
            parse_gap_plan(_GAP_PLAN_MISSING_STRATEGY)
        self.assertIn("strategy", str(ctx.exception).lower())

    def test_parser_rejects_missing_verification_instead_of_defaulting_to_test(self):
        with self.assertRaises(PlanningError) as ctx:
            parse_gap_plan(_GAP_PLAN_MISSING_VERIFICATION)
        self.assertIn("verify", str(ctx.exception).lower())

    def test_parser_rejects_when_both_tags_missing(self):
        with self.assertRaises(PlanningError):
            parse_gap_plan(_GAP_PLAN_NO_TAGS)

    def test_parser_succeeds_when_both_tags_present(self):
        criteria = parse_gap_plan(_GAP_PLAN_WITH_TAGS)
        self.assertEqual(2, len(criteria))
        self.assertEqual("test", criteria[0].verification)
        self.assertEqual("direct", criteria[0].implementation_strategy)
        self.assertEqual("manual", criteria[1].verification)
        self.assertEqual("direct", criteria[1].implementation_strategy)


class ExtractFunctionsReturnNoneForMissingTagsTests(unittest.TestCase):
    """extract_verification_mode and extract_strategy return None for missing tags."""

    def test_extract_verification_returns_none_when_no_tag(self):
        criterion = "- [ ] Criterion without a verify tag <!-- why: missing -->"
        self.assertIsNone(lib.extract_verification_mode(criterion))

    def test_extract_strategy_returns_none_when_no_tag(self):
        criterion = "- [ ] Criterion without a strategy tag <!-- why: missing; verify: test -->"
        self.assertIsNone(lib.extract_strategy(criterion))

    def test_extract_verification_parses_test_tag(self):
        criterion = "- [ ] Thing <!-- verify: test; strategy: direct -->"
        self.assertEqual("test", lib.extract_verification_mode(criterion))

    def test_extract_strategy_parses_direct_tag(self):
        criterion = "- [ ] Thing <!-- verify: test; strategy: direct -->"
        self.assertEqual("direct", lib.extract_strategy(criterion))

    def test_extract_strategy_parses_tdd_tag(self):
        criterion = "- [ ] Thing <!-- verify: test; strategy: tdd -->"
        self.assertEqual("tdd", lib.extract_strategy(criterion))


# ---------------------------------------------------------------------------
# Strategy dispatch independence
# ---------------------------------------------------------------------------


def _make_frame(*, strategy="tdd", status="pending", verification="test"):
    return lib.CriterionFrame(
        ticket="SA-1",
        criterion="- [ ] do the thing",
        plan_context="ctx",
        test_files=["tests/test_example.py"] if verification != "manual" else None,
        test_names=["tests::example"] if verification != "manual" else None,
        status=status,
        origin="ticket",
        verification=verification,
        strategy=strategy,
    )


class DirectStrategyIndependenceTests(unittest.TestCase):
    """Direct strategy must not enter TDD-specific states."""

    def test_direct_strategy_never_enters_test_written_state(self):
        """A direct-strategy criterion's status must never be 'test-written'."""
        frame = _make_frame(strategy="direct", status="pending")
        phases_visited = []

        def capture_status(*args, **kwargs):
            phases_visited.append(frame.status)

        with (
            mock.patch.object(lib, "load_stack", return_value=[frame]),
            mock.patch(
                "ticket_pipeline.lib.implement.run_implement_direct_with_refine",
                return_value=["src/example.py"],
            ),
        ):
            try:
                next_step.step("model", {"build_cmd": "true"}, False, lib.PIPELINE_CONFIG_FILE)
            except SystemExit:
                pass

        self.assertNotIn("test-written", phases_visited)
        self.assertNotEqual("test-written", frame.status)

    def test_direct_strategy_does_not_invoke_test_generation(self):
        """Direct strategy must not call the TDD write-test function."""
        frame = _make_frame(strategy="direct", status="pending")
        with (
            mock.patch.object(lib, "load_stack", return_value=[frame]),
            mock.patch(
                "ticket_pipeline.lib.implement.run_implement_direct_with_refine",
                return_value=["src/example.py"],
            ),
            mock.patch.object(tdd_strategy, "do_write_test") as write_test_mock,
        ):
            try:
                next_step.step("model", {"build_cmd": "true"}, False, lib.PIPELINE_CONFIG_FILE)
            except SystemExit:
                pass

        write_test_mock.assert_not_called()

    def test_direct_strategy_dispatches_to_direct_implement(self):
        """Direct strategy must invoke run_implement_direct_with_refine."""
        frame = _make_frame(strategy="direct", status="pending")
        with (
            mock.patch.object(lib, "load_stack", return_value=[frame]),
            mock.patch(
                "ticket_pipeline.lib.implement.run_implement_direct_with_refine",
                return_value=["src/example.py"],
            ) as impl_mock,
        ):
            try:
                next_step.step("model", {"build_cmd": "true"}, False, lib.PIPELINE_CONFIG_FILE)
            except SystemExit:
                pass

        impl_mock.assert_called_once()


class RefactorStrategyIndependenceTests(unittest.TestCase):
    """Refactor strategy must not generate a failing test."""

    def test_refactor_strategy_does_not_generate_a_failing_test(self):
        """Refactor strategy invokes do_refactor_setup, not TDD write-test."""
        frame = _make_frame(strategy="refactor", status="pending", verification="refactor")
        with (
            mock.patch.object(lib, "load_stack", return_value=[frame]),
            mock.patch.object(refactor_strategy, "do_refactor_setup") as refactor_mock,
            mock.patch.object(tdd_strategy, "do_write_test") as write_test_mock,
        ):
            next_step.step("model", {"build_cmd": "true"}, False, lib.PIPELINE_CONFIG_FILE)

        refactor_mock.assert_called_once()
        write_test_mock.assert_not_called()


class ManualStrategyIndependenceTests(unittest.TestCase):
    """Legacy manual strategy frames delegate to direct implementation."""

    def test_manual_strategy_invokes_ai_implementation(self):
        """Backward-compatible manual frames must call direct implementation."""
        frame = _make_frame(strategy="manual", status="pending", verification="manual")
        with (
            mock.patch.object(lib, "load_stack", return_value=[frame]),
            mock.patch(
                "ticket_pipeline.lib.implement.run_implement_direct_with_refine",
                return_value=["src/example.py"],
            ) as impl_mock,
            mock.patch.object(tdd_strategy, "do_write_test") as write_test_mock,
        ):
            try:
                next_step.step("model", {"build_cmd": "true"}, False, lib.PIPELINE_CONFIG_FILE)
            except SystemExit:
                pass

        impl_mock.assert_called_once()
        write_test_mock.assert_not_called()


class TDDStrategyRetentionTests(unittest.TestCase):
    """TDD strategy must continue to provide its test-first behavior."""

    def test_tdd_strategy_retains_red_green_execution(self):
        """TDD strategy dispatches pending → do_write_test (the test-first entry point)."""
        frame = _make_frame(strategy="tdd", status="pending")
        with (
            mock.patch.object(lib, "load_stack", return_value=[frame]),
            mock.patch.object(tdd_strategy, "do_write_test") as write_test_mock,
        ):
            next_step.step("model", {"build_cmd": "true"}, False, lib.PIPELINE_CONFIG_FILE)

        write_test_mock.assert_called_once()


# ---------------------------------------------------------------------------
# Artifact naming: legacy .tdd-plan.md backward compatibility
# ---------------------------------------------------------------------------


class LegacyPlanFileTests(unittest.TestCase):
    """The deprecated .tdd-plan.md filename must remain readable during migration."""

    def test_legacy_tdd_plan_filename_is_supported_during_migration(self):
        """_resolve_plan_file falls back to .tdd-plan.md if .implementation-plan.md absent."""
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            impl_plan = tmp_path / ".implementation-plan.md"
            legacy_plan = tmp_path / ".tdd-plan.md"
            legacy_plan.write_text("# legacy plan\n", encoding="utf-8")

            with (
                patch.object(lib, "PLAN_FILE", impl_plan),
                patch.object(lib, "LEGACY_PLAN_FILE", legacy_plan),
            ):
                resolved = lib._resolve_plan_file()

            self.assertIsNotNone(resolved)
            self.assertEqual(resolved, legacy_plan)
            self.assertEqual(resolved.read_text(encoding="utf-8"), "# legacy plan\n")

    def test_new_plan_file_preferred_over_legacy(self):
        """_resolve_plan_file returns .implementation-plan.md when it exists."""
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            impl_plan = tmp_path / ".implementation-plan.md"
            legacy_plan = tmp_path / ".tdd-plan.md"
            impl_plan.write_text("# new plan\n", encoding="utf-8")
            legacy_plan.write_text("# legacy plan\n", encoding="utf-8")

            with (
                patch.object(lib, "PLAN_FILE", impl_plan),
                patch.object(lib, "LEGACY_PLAN_FILE", legacy_plan),
            ):
                resolved = lib._resolve_plan_file()

            self.assertEqual(resolved, impl_plan)

    def test_resolve_plan_file_returns_none_when_neither_exists(self):
        """_resolve_plan_file returns None when no plan file is present."""
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            impl_plan = tmp_path / ".implementation-plan.md"
            legacy_plan = tmp_path / ".tdd-plan.md"

            with (
                patch.object(lib, "PLAN_FILE", impl_plan),
                patch.object(lib, "LEGACY_PLAN_FILE", legacy_plan),
            ):
                resolved = lib._resolve_plan_file()

            self.assertIsNone(resolved)


# ---------------------------------------------------------------------------
# End-to-end strategy dispatch: verify=test + strategy=direct
# ---------------------------------------------------------------------------


class EndToEndDirectWithTestVerificationTests(unittest.TestCase):
    """
    Proves that a criterion with verification=test and strategy=direct:
    1. Does not invoke test generation (do_write_test).
    2. Does not require a newly failing test.
    3. Invokes the direct implementation strategy.
    4. Does not enter the 'test-written' state.
    """

    def test_direct_strategy_with_test_verification_full_dispatch(self):
        frame = lib.CriterionFrame(
            ticket="SA-1",
            criterion="- [ ] Change observable behavior verified by tests",
            plan_context="Implement the behavior. Tests will confirm it.",
            test_files=None,
            test_names=None,
            status="pending",
            origin="ticket",
            verification="test",
            strategy="direct",
        )
        direct_impl_called = []
        write_test_called = []

        def fake_direct_impl(*args, **kwargs):
            direct_impl_called.append(True)
            return ["src/feature.py"]

        with (
            mock.patch.object(lib, "load_stack", return_value=[frame]),
            mock.patch(
                "ticket_pipeline.lib.implement.run_implement_direct_with_refine",
                side_effect=fake_direct_impl,
            ),
            mock.patch.object(
                tdd_strategy,
                "do_write_test",
                side_effect=lambda *a, **kw: write_test_called.append(True),
            ),
        ):
            try:
                next_step.step("model", {"build_cmd": "true"}, False, lib.PIPELINE_CONFIG_FILE)
            except SystemExit:
                pass

        # 1. Test generation was NOT invoked
        self.assertEqual([], write_test_called, "do_write_test must not be called")
        # 2. Direct implementation was invoked
        self.assertEqual(
            [True],
            direct_impl_called,
            "run_implement_direct_with_refine must be called",
        )
        # 3. Frame was never in 'test-written' state
        self.assertNotEqual("test-written", frame.status)


# ---------------------------------------------------------------------------
# TDD strategy: mechanical path check on done-gating
# ---------------------------------------------------------------------------


class TDDStrategyPathCheckTests(unittest.TestCase):
    """TDD strategy must not accept 'done' when the diff skips plan-referenced files."""

    def test_tdd_recheck_not_done_when_diff_misses_plan_referenced_file(self):
        """recheck_test_frame must not mark done when no plan-referenced path is in the diff."""
        frame = lib.CriterionFrame(
            ticket="SA-1",
            criterion="- [ ] do the thing <!-- verify: test; strategy: tdd -->",
            plan_context="Edit `src/ticket_pipeline/lib/pipeline_lib.py`.",
            test_files=["tests/test_example.py"],
            test_names=["test_example"],
            status="test-written",
            origin="ticket",
            verification="test",
            strategy="tdd",
        )
        frame.unconfirmed_tests = []

        results_all_green = [
            type("R", (), {"returncode": 0})(),
        ]

        with (
            mock.patch.object(lib, "run_scoped_tests", return_value=results_all_green),
            mock.patch.object(
                lib,
                "extract_referenced_paths",
                return_value=["src/ticket_pipeline/lib/pipeline_lib.py"],
            ),
            mock.patch.object(
                lib,
                "git_changed_files",
                # diff only touched __init__.py — not the plan-referenced file
                return_value=["src/ticket_pipeline/lib/__init__.py"],
            ),
            mock.patch.object(lib, "save_stack"),
        ):
            with self.assertRaises(SystemExit):
                tdd_strategy.recheck_test_frame([], frame, _make_ctx())

        self.assertNotEqual("done", frame.status)

    def test_tdd_recheck_done_when_diff_touches_plan_referenced_file(self):
        """recheck_test_frame marks done when the diff includes a plan-referenced path."""
        frame = lib.CriterionFrame(
            ticket="SA-1",
            criterion="- [ ] do the thing <!-- verify: test; strategy: tdd -->",
            plan_context="Edit `src/ticket_pipeline/lib/pipeline_lib.py`.",
            test_files=["tests/test_example.py"],
            test_names=["test_example"],
            status="test-written",
            origin="ticket",
            verification="test",
            strategy="tdd",
        )
        frame.unconfirmed_tests = []

        results_all_green = [
            type("R", (), {"returncode": 0})(),
        ]

        with (
            mock.patch.object(lib, "run_scoped_tests", return_value=results_all_green),
            mock.patch.object(
                lib,
                "extract_referenced_paths",
                return_value=["src/ticket_pipeline/lib/pipeline_lib.py"],
            ),
            mock.patch.object(
                lib,
                "git_changed_files",
                return_value=["src/ticket_pipeline/lib/pipeline_lib.py"],
            ),
            mock.patch.object(lib, "save_stack"),
        ):
            tdd_strategy.recheck_test_frame([], frame, _make_ctx())

        self.assertEqual("done", frame.status)


def _make_ctx(**kwargs):
    """Return a minimal StepContext for unit tests."""
    defaults = dict(
        model="test-model",
        step_models={},
        commands={"build_cmd": "true"},
        config_path=lib.PIPELINE_CONFIG_FILE,
        continuous=False,
        max_attempts=1,
        retry_policy=None,
        accept_green=False,
        accept_manual=False,
        accept_no_test=False,
        skip_implementation=False,
        allow_compile=False,
        reset_on_retry=False,
        git_cfg=None,
    )
    defaults.update(kwargs)
    return lib.StepContext(**defaults)


if __name__ == "__main__":
    unittest.main()
