import unittest
import inspect
from unittest import mock

from ticket_pipeline import next_step
from ticket_pipeline.lib import pipeline_lib as lib
from ticket_pipeline.strategies import refactor as refactor_strategy
from ticket_pipeline.strategies import tdd as tdd_strategy


class StrategyDispatchTests(unittest.TestCase):
    def _frame(self, *, strategy="tdd", status="pending", verification="test"):
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

    def test_tdd_strategy_dispatches_pending_to_write_test(self):
        frame = self._frame(strategy="tdd", status="pending")
        with (
            mock.patch.object(lib, "load_stack", return_value=[frame]),
            mock.patch.object(tdd_strategy, "do_write_test") as write_test,
        ):
            next_step.step(
                "model", {"build_cmd": "true"}, False, lib.PIPELINE_CONFIG_FILE
            )
        write_test.assert_called_once()

    def test_tdd_strategy_dispatches_test_written_to_recheck(self):
        frame = self._frame(strategy="tdd", status="test-written")
        with (
            mock.patch.object(lib, "load_stack", return_value=[frame]),
            mock.patch.object(tdd_strategy, "recheck_test_frame") as recheck,
        ):
            next_step.step(
                "model", {"build_cmd": "true"}, False, lib.PIPELINE_CONFIG_FILE
            )
        recheck.assert_called_once()

    def test_tdd_strategy_retains_red_green_execution(self):
        """TDD strategy must still invoke do_write_test (test-first) when selected."""
        frame = self._frame(strategy="tdd", status="pending", verification="test")
        with (
            mock.patch.object(lib, "load_stack", return_value=[frame]),
            mock.patch.object(tdd_strategy, "do_write_test") as write_test,
        ):
            next_step.step(
                "model", {"build_cmd": "true"}, False, lib.PIPELINE_CONFIG_FILE
            )
        write_test.assert_called_once()

    def test_direct_strategy_dispatches_pending_to_implement(self):
        frame = self._frame(strategy="direct", status="pending")
        with (
            mock.patch.object(lib, "load_stack", return_value=[frame]),
            mock.patch(
                "ticket_pipeline.lib.implement.run_implement_direct_with_refine",
                return_value=["src/example.py"],
            ),
        ):
            with self.assertRaises(SystemExit):
                next_step.step(
                    "model", {"build_cmd": "true"}, False, lib.PIPELINE_CONFIG_FILE
                )

    def test_direct_strategy_never_enters_test_written_state(self):
        """Direct strategy must not set status='test-written' - that is TDD-only."""
        frame = self._frame(strategy="direct", status="pending", verification="test")
        with (
            mock.patch.object(lib, "load_stack", return_value=[frame]),
            mock.patch(
                "ticket_pipeline.lib.implement.run_implement_direct_with_refine",
                return_value=["src/example.py"],
            ),
        ):
            with self.assertRaises(SystemExit):
                next_step.step(
                    "model", {"build_cmd": "true"}, False, lib.PIPELINE_CONFIG_FILE
                )
        # Direct strategy's advance() moves to "implemented", never "test-written"
        self.assertNotEqual("test-written", frame.status)

    def test_manual_strategy_uses_manual_handler(self):
        frame = self._frame(strategy="manual", status="pending", verification="manual")
        with (
            mock.patch.object(lib, "load_stack", return_value=[frame]),
            mock.patch.object(lib, "extract_referenced_paths", return_value=[]),
            mock.patch.object(lib, "git_changed_files", return_value=[]),
            mock.patch(
                "ticket_pipeline.strategies.manual.do_await_manual_impl"
            ) as manual_pause,
        ):
            next_step.step(
                "model", {"build_cmd": "true"}, False, lib.PIPELINE_CONFIG_FILE
            )
        manual_pause.assert_called_once()

    def test_manual_strategy_does_not_invoke_ai_implementation(self):
        """Manual strategy's initial advance step must not invoke AI implementation."""
        frame = self._frame(strategy="manual", status="pending", verification="manual")
        with (
            mock.patch.object(lib, "load_stack", return_value=[frame]),
            mock.patch.object(lib, "extract_referenced_paths", return_value=[]),
            mock.patch.object(lib, "git_changed_files", return_value=[]),
            mock.patch(
                "ticket_pipeline.lib.implement.run_implement_direct_with_refine"
            ) as ai_impl,
            mock.patch(
                "ticket_pipeline.strategies.manual.do_await_manual_impl"
            ) as _manual_pause,
        ):
            next_step.step(
                "model", {"build_cmd": "true"}, False, lib.PIPELINE_CONFIG_FILE
            )
        ai_impl.assert_not_called()

    def test_refactor_strategy_uses_refactor_handler(self):
        frame = self._frame(
            strategy="refactor", status="pending", verification="refactor"
        )
        with (
            mock.patch.object(lib, "load_stack", return_value=[frame]),
            mock.patch.object(refactor_strategy, "do_refactor_setup") as refactor,
        ):
            next_step.step(
                "model", {"build_cmd": "true"}, False, lib.PIPELINE_CONFIG_FILE
            )
        refactor.assert_called_once()

    def test_refactor_strategy_does_not_generate_a_failing_test(self):
        """Refactor strategy must not invoke the TDD test-writer."""
        frame = self._frame(
            strategy="refactor", status="pending", verification="refactor"
        )
        with (
            mock.patch.object(lib, "load_stack", return_value=[frame]),
            mock.patch.object(refactor_strategy, "do_refactor_setup") as _refactor_setup,
            mock.patch.object(tdd_strategy, "do_write_test") as write_test,
        ):
            next_step.step(
                "model", {"build_cmd": "true"}, False, lib.PIPELINE_CONFIG_FILE
            )
        write_test.assert_not_called()

    def test_unknown_strategy_dies(self):
        frame = self._frame(strategy="unknown", status="pending")
        with (
            mock.patch.object(lib, "load_stack", return_value=[frame]),
            mock.patch.object(lib, "die_with_log", side_effect=RuntimeError("boom")),
        ):
            with self.assertRaises(RuntimeError):
                next_step.step(
                    "model", {"build_cmd": "true"}, False, lib.PIPELINE_CONFIG_FILE
                )

    def test_manual_test_refused_for_direct_strategy(self):
        frame = self._frame(strategy="direct", status="pending", verification="test")
        with (
            mock.patch.object(lib, "load_stack", return_value=[frame]),
            mock.patch.object(
                lib, "die_with_log", side_effect=RuntimeError("bad manual-test")
            ) as die_with_log,
        ):
            with self.assertRaisesRegex(RuntimeError, "bad manual-test"):
                next_step.step(
                    "model",
                    {"build_cmd": "true", "test_compile_cmd": "true"},
                    False,
                    lib.PIPELINE_CONFIG_FILE,
                    manual_test=True,
                    manual_test_refs=["tests/test_example.py::tests::example"],
                )
        self.assertIn("not valid for strategy='direct'", die_with_log.call_args.args[1])

    def test_step_manual_skip_dispatch_has_no_verification_checks(self):
        source = inspect.getsource(next_step.step)
        self.assertNotIn("frame.verification", source)

    def test_end_to_end_test_verify_direct_strategy(self):
        """
        End-to-end dispatch: a criterion with verify:test and strategy:direct must:
        1. not invoke the TDD test-writer;
        2. not require a newly failing test;
        3. invoke the direct implementation strategy;
        4. not enter test-written state.

        The acceptance check (run_scoped_tests) is mocked because this test
        focuses on strategy dispatch, not the test runner.
        """
        frame = self._frame(strategy="direct", status="pending", verification="test")
        with (
            mock.patch.object(lib, "load_stack", return_value=[frame]),
            mock.patch.object(tdd_strategy, "do_write_test") as tdd_write_test,
            mock.patch(
                "ticket_pipeline.lib.implement.run_implement_direct_with_refine",
                return_value=["src/new_feature.py"],
            ) as direct_impl,
        ):
            with self.assertRaises(SystemExit):
                next_step.step(
                    "model", {"build_cmd": "true"}, False, lib.PIPELINE_CONFIG_FILE
                )

        # 1 & 2: TDD test-writer must not have been called
        tdd_write_test.assert_not_called()
        # 3: Direct implementation was invoked
        direct_impl.assert_called_once()
        # 4: Status is not "test-written" (that is a TDD-only state)
        self.assertNotEqual("test-written", frame.status)


if __name__ == "__main__":
    unittest.main()
