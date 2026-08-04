import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scaffold_cli import cli
from ticket_pipeline import next_step, status
from ticket_pipeline.lib import pipeline_lib as lib
from ticket_pipeline.lib.retry import (
    EndlessRetryPolicy,
    FixedBudgetPolicy,
    resolve_retry_policy,
)
from ticket_pipeline.strategies import tdd as tdd_strategy


class NextStepDispatchTests(unittest.TestCase):
    def _frame(self, *, verification="test", status="test-written", strategy=None):
        strategy = strategy or {
            "manual": "direct",
            "refactor": "refactor",
        }.get(verification, "tdd")
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

    def test_test_written_dispatches_to_recheck_phase(self):
        frame = self._frame()
        with (
            mock.patch.object(lib, "load_stack", return_value=[frame]),
            mock.patch.object(tdd_strategy, "recheck_test_frame") as recheck,
        ):
            next_step.step("model", {"build_cmd": "true"}, False, lib.PIPELINE_CONFIG_FILE)
        recheck.assert_called_once()

    def test_manual_verification_dispatches_to_direct_implementor(self):
        frame = self._frame(verification="manual", status="pending", strategy="direct")
        with (
            mock.patch.object(lib, "load_stack", return_value=[frame]),
            mock.patch(
                "ticket_pipeline.lib.implement.run_implement_direct_with_refine",
                return_value=["src/example.py"],
            ) as run_direct,
        ):
            with self.assertRaises(SystemExit):
                next_step.step("model", {"build_cmd": "true"}, False, lib.PIPELINE_CONFIG_FILE)
        run_direct.assert_called_once()

    def test_baseline_confirmed_dispatches_to_implementation_phase(self):
        frame = self._frame(verification="refactor", status=lib.BASELINE_CONFIRMED_STATUS)
        with (
            mock.patch.object(lib, "load_stack", return_value=[frame]),
            mock.patch.object(lib, "run_scoped_tests", return_value=[]),
            mock.patch.object(lib, "extract_referenced_paths", return_value=["src/example.py"]),
            mock.patch.object(lib, "git_changed_files", return_value=[]),
            mock.patch("ticket_pipeline.strategies.refactor.implement") as run_impl,
        ):
            with self.assertRaises(SystemExit) as cm:
                next_step.step("model", {"build_cmd": "true"}, False, lib.PIPELINE_CONFIG_FILE)
        self.assertEqual(0, cm.exception.code)
        run_impl.assert_called_once()


class NextStepContinuousModeTests(unittest.TestCase):
    def _test_frame(self, *, status="test-written", unconfirmed_tests=None):
        return lib.CriterionFrame(
            ticket="SA-1",
            criterion="- [ ] do the thing",
            plan_context="ctx",
            test_files=["tests/test_example.py"],
            test_names=["tests::example"],
            status=status,
            origin="ticket",
            verification="test",
            strategy="tdd",
            unconfirmed_tests=unconfirmed_tests or [],
        )

    def _manual_frame(self):
        return lib.CriterionFrame(
            ticket="SA-1",
            criterion="- [ ] update docs",
            plan_context="ctx",
            test_files=None,
            test_names=None,
            status="pending",
            origin="ticket",
            verification="manual",
            strategy="direct",
        )

    def test_implementation_phase_continues_under_continuous(self):
        frame = self._test_frame()
        red = subprocess.CompletedProcess(args=["test"], returncode=1, stdout="", stderr="")
        ctx = lib.StepContext(
            model="model",
            step_models={},
            commands={"build_cmd": "true"},
            config_path=lib.PIPELINE_CONFIG_FILE,
            continuous=True,
            max_attempts=2,
            accept_green=False,
            accept_manual=False,
            accept_no_test=False,
            skip_implementation=False,
            git_cfg=None,
        )
        with (
            mock.patch.object(lib, "run_scoped_tests", return_value=[red]),
            mock.patch("ticket_pipeline.strategies.tdd.implement") as implement,
        ):
            tdd_strategy.recheck_test_frame([frame], frame, ctx)
        implement.assert_called_once()

    def test_implementation_phase_exits_after_single_phase_without_continuous(self):
        frame = self._test_frame()
        red = subprocess.CompletedProcess(args=["test"], returncode=1, stdout="", stderr="")
        ctx = lib.StepContext(
            model="model",
            step_models={},
            commands={"build_cmd": "true"},
            config_path=lib.PIPELINE_CONFIG_FILE,
            continuous=False,
            max_attempts=2,
            accept_green=False,
            accept_manual=False,
            accept_no_test=False,
            skip_implementation=False,
            git_cfg=None,
        )
        with (
            mock.patch.object(lib, "run_scoped_tests", return_value=[red]),
            mock.patch("ticket_pipeline.strategies.tdd.implement") as implement,
        ):
            tdd_strategy.recheck_test_frame([frame], frame, ctx)
        implement.assert_called_once()

    def test_continuous_still_pauses_for_green_unconfirmed(self):
        frame = self._test_frame(
            status=tdd_strategy.GREEN_UNCONFIRMED_STATUS,
            unconfirmed_tests=["tests::example"],
        )
        green = subprocess.CompletedProcess(args=["test"], returncode=0, stdout="", stderr="")
        with (
            mock.patch.object(lib, "load_stack", return_value=[frame]),
            mock.patch.object(lib, "run_scoped_tests", return_value=[green]),
            mock.patch.object(lib, "save_stack"),
        ):
            with self.assertRaises(SystemExit) as cm:
                next_step.step(
                    "model",
                    {"build_cmd": "true"},
                    True,
                    lib.PIPELINE_CONFIG_FILE,
                )
        self.assertEqual(0, cm.exception.code)

    def test_continuous_direct_strategy_implements_manual_verification(self):
        frame = self._manual_frame()
        with mock.patch(
            "ticket_pipeline.lib.implement.run_implement_direct_with_refine",
            return_value=["docs/guide.md"],
        ) as run_direct:
            from ticket_pipeline.strategies import direct as direct_strategy

            direct_strategy.advance(
                [frame],
                frame,
                lib.StepContext(
                    model="model",
                    step_models={},
                    commands={"build_cmd": "true"},
                    config_path=lib.PIPELINE_CONFIG_FILE,
                    continuous=True,
                    max_attempts=2,
                ),
            )
        run_direct.assert_called_once()


class CliHelpTests(unittest.TestCase):
    def test_top_level_help_omits_retired_commands(self):
        stdout = io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["scaffold", "--help"]),
            mock.patch("sys.stdout", stdout),
        ):
            with self.assertRaises(SystemExit) as cm:
                cli.main()
        self.assertEqual(0, cm.exception.code)
        help_text = stdout.getvalue()
        self.assertIn("next-step", help_text)
        self.assertNotIn("implement-step", help_text)
        self.assertNotIn("\n  drive", help_text)

    def test_next_step_help_includes_manual_test_flags(self):
        stdout = io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["scaffold", "next-step", "--help"]),
            mock.patch("sys.stdout", stdout),
        ):
            with self.assertRaises(SystemExit) as cm:
                cli.main()
        self.assertEqual(0, cm.exception.code)
        help_text = stdout.getvalue()
        self.assertIn("--manual-test", help_text)
        self.assertIn("--manual-test-ref", help_text)
        self.assertIn("--skip-implementation", help_text)
        self.assertIn("--retry-policy", help_text)


class RetryPolicyResolutionTests(unittest.TestCase):
    def test_resolve_retry_policy_defaults_to_none(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(resolve_retry_policy(Path(d) / "missing.toml", None, 3))

    def test_resolve_retry_policy_supports_endless_from_config(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "pipeline.toml"
            cfg.write_text("[retry]\npolicy = 'endless'\n", encoding="utf-8")
            policy = resolve_retry_policy(cfg, None, 3)
        self.assertIsInstance(policy, EndlessRetryPolicy)

    def test_resolve_retry_policy_supports_cli_fixed_budget(self):
        with tempfile.TemporaryDirectory() as d:
            policy = resolve_retry_policy(Path(d) / "missing.toml", "fixed-budget", 5)
        self.assertIsInstance(policy, FixedBudgetPolicy)
        self.assertEqual(5, policy.max_attempts)


class ManualTestModeTests(unittest.TestCase):
    def _frame(self, *, origin="ticket"):
        return lib.CriterionFrame(
            ticket="SA-1",
            criterion="- [ ] do the thing",
            plan_context="ctx",
            test_files=None,
            test_names=None,
            status="pending",
            origin=origin,
            verification="test",
        )

    def test_pending_manual_test_sets_test_written_and_runs_implementor_by_default(
        self,
    ):
        frame = self._frame()
        red = subprocess.CompletedProcess(args=["test"], returncode=1, stdout="", stderr="")
        with (
            mock.patch.object(lib, "load_stack", return_value=[frame]),
            mock.patch.object(
                lib,
                "run_command",
                return_value=subprocess.CompletedProcess(
                    args=["compile"], returncode=0, stdout="", stderr=""
                ),
            ),
            mock.patch.object(lib, "run_scoped_tests", return_value=[red]),
            mock.patch.object(lib, "save_stack") as save_stack,
            mock.patch.object(tdd_strategy, "recheck_test_frame") as recheck,
            mock.patch.object(tdd_strategy, "do_write_test") as do_write_test,
        ):
            next_step.step(
                "model",
                {"build_cmd": "true", "test_compile_cmd": "true"},
                False,
                lib.PIPELINE_CONFIG_FILE,
                manual_test=True,
                manual_test_refs=["tests/test_example.py::tests::example"],
            )
        self.assertEqual("test-written", frame.status)
        self.assertEqual(["tests/test_example.py"], frame.test_files)
        self.assertEqual(["tests::example"], frame.test_names)
        save_stack.assert_called()
        recheck.assert_called_once()
        do_write_test.assert_not_called()

    def test_pending_manual_test_with_skip_implementation_pauses_for_manual_impl(self):
        frame = self._frame()
        red = subprocess.CompletedProcess(args=["test"], returncode=1, stdout="", stderr="")
        with (
            mock.patch.object(lib, "load_stack", return_value=[frame]),
            mock.patch.object(
                lib,
                "run_command",
                return_value=subprocess.CompletedProcess(
                    args=["compile"], returncode=0, stdout="", stderr=""
                ),
            ),
            mock.patch.object(lib, "run_scoped_tests", return_value=[red]),
            mock.patch.object(lib, "save_stack"),
            mock.patch.object(tdd_strategy, "do_await_impl") as await_impl,
            mock.patch.object(tdd_strategy, "recheck_test_frame") as recheck,
        ):
            next_step.step(
                "model",
                {"build_cmd": "true", "test_compile_cmd": "true"},
                False,
                lib.PIPELINE_CONFIG_FILE,
                manual_test=True,
                skip_implementation=True,
                manual_test_refs=["tests/test_example.py::tests::example"],
            )
        await_impl.assert_called_once()
        recheck.assert_not_called()

    def test_manual_test_rejects_bad_ref_format(self):
        frame = self._frame()
        with (
            mock.patch.object(lib, "load_stack", return_value=[frame]),
            mock.patch.object(
                lib, "die_with_log", side_effect=RuntimeError("bad ref")
            ) as die_with_log,
        ):
            with self.assertRaisesRegex(RuntimeError, "bad ref"):
                next_step.step(
                    "model",
                    {"build_cmd": "true", "test_compile_cmd": "true"},
                    False,
                    lib.PIPELINE_CONFIG_FILE,
                    manual_test=True,
                    manual_test_refs=["bad-ref"],
                )
        die_with_log.assert_called_once()
        self.assertIn("Invalid manual test reference", die_with_log.call_args.args[1])

    def test_manual_test_compile_failure_dies(self):
        frame = self._frame()
        with (
            mock.patch.object(lib, "load_stack", return_value=[frame]),
            mock.patch.object(
                lib,
                "run_command",
                return_value=subprocess.CompletedProcess(
                    args=["compile"], returncode=1, stdout="", stderr=""
                ),
            ),
            mock.patch.object(
                lib, "die_with_log", side_effect=RuntimeError("compile fail")
            ) as die_with_log,
        ):
            with self.assertRaisesRegex(RuntimeError, "compile fail"):
                next_step.step(
                    "model",
                    {"build_cmd": "true", "test_compile_cmd": "false"},
                    False,
                    lib.PIPELINE_CONFIG_FILE,
                    manual_test=True,
                    manual_test_refs=["tests/test_example.py::tests::example"],
                )
        self.assertIn("Manual test compile gate failed", die_with_log.call_args.args[1])

    def test_manual_test_green_non_ticket_origin_pauses_unconfirmed(self):
        frame = self._frame(origin="review")
        green = subprocess.CompletedProcess(args=["test"], returncode=0, stdout="", stderr="")
        with (
            mock.patch.object(lib, "load_stack", return_value=[frame]),
            mock.patch.object(
                lib,
                "run_command",
                return_value=subprocess.CompletedProcess(
                    args=["compile"], returncode=0, stdout="", stderr=""
                ),
            ),
            mock.patch.object(lib, "run_scoped_tests", return_value=[green]),
            mock.patch.object(lib, "save_stack"),
            mock.patch.object(tdd_strategy, "do_await_green_unconfirmed") as await_unconfirmed,
        ):
            next_step.step(
                "model",
                {"build_cmd": "true", "test_compile_cmd": "true"},
                False,
                lib.PIPELINE_CONFIG_FILE,
                manual_test=True,
                manual_test_refs=["tests/test_example.py::tests::example"],
            )
        self.assertEqual(tdd_strategy.GREEN_UNCONFIRMED_STATUS, frame.status)
        self.assertEqual(["tests::example"], frame.unconfirmed_tests)
        await_unconfirmed.assert_called_once()

    def test_manual_test_accepts_nested_qualified_test_name(self):
        frame = self._frame()
        nested_name = "tests::submodule::ClassName::test_method"
        red = subprocess.CompletedProcess(args=["test"], returncode=1, stdout="", stderr="")
        with (
            mock.patch.object(lib, "load_stack", return_value=[frame]),
            mock.patch.object(
                lib,
                "run_command",
                return_value=subprocess.CompletedProcess(
                    args=["compile"], returncode=0, stdout="", stderr=""
                ),
            ),
            mock.patch.object(lib, "run_scoped_tests", return_value=[red]),
            mock.patch.object(lib, "save_stack"),
            mock.patch.object(tdd_strategy, "recheck_test_frame"),
        ):
            next_step.step(
                "model",
                {"build_cmd": "true", "test_compile_cmd": "true"},
                False,
                lib.PIPELINE_CONFIG_FILE,
                manual_test=True,
                manual_test_refs=[f"tests/test_example.py::{nested_name}"],
            )
        self.assertEqual([nested_name], frame.test_names)


class SkipTestModeTests(unittest.TestCase):
    def _frame(self, *, status="pending", verification="test", strategy="tdd"):
        return lib.CriterionFrame(
            ticket="SA-1",
            criterion="- [ ] do the thing",
            plan_context="ctx",
            test_files=None,
            test_names=None,
            status=status,
            origin="ticket",
            verification=verification,
            strategy=strategy,
        )

    def test_pending_skip_test_hands_off_to_direct_implementor(self):
        frame = self._frame()
        with (
            mock.patch.object(lib, "load_stack", return_value=[frame]),
            mock.patch(
                "ticket_pipeline.lib.implement.run_implement_direct_with_refine",
                return_value=["src/example.py"],
            ) as run_direct,
            mock.patch.object(tdd_strategy, "_handle_no_test_written") as handle_no_test,
        ):
            next_step.step(
                "model",
                {"build_cmd": "true"},
                False,
                lib.PIPELINE_CONFIG_FILE,
                skip_test=True,
            )
        run_direct.assert_called_once()
        handle_no_test.assert_called_once()

    def test_skip_test_rejects_non_pending_frame(self):
        frame = self._frame(status="test-written")
        with (
            mock.patch.object(lib, "load_stack", return_value=[frame]),
            mock.patch.object(
                lib, "die_with_log", side_effect=RuntimeError("bad skip")
            ) as die_with_log,
        ):
            with self.assertRaisesRegex(RuntimeError, "bad skip"):
                next_step.step(
                    "model",
                    {"build_cmd": "true"},
                    False,
                    lib.PIPELINE_CONFIG_FILE,
                    skip_test=True,
                )
        self.assertIn("only applies when the top frame is pending", die_with_log.call_args.args[1])

    def test_skip_test_rejects_non_supported_strategy(self):
        frame = self._frame(verification="manual", strategy="refactor")
        with (
            mock.patch.object(lib, "load_stack", return_value=[frame]),
            mock.patch.object(
                lib, "die_with_log", side_effect=RuntimeError("bad mode")
            ) as die_with_log,
        ):
            with self.assertRaisesRegex(RuntimeError, "bad mode"):
                next_step.step(
                    "model",
                    {"build_cmd": "true"},
                    False,
                    lib.PIPELINE_CONFIG_FILE,
                    skip_test=True,
                )
        self.assertIn("not valid for strategy='refactor'", die_with_log.call_args.args[1])

    def test_skip_test_rejects_skip_implementation_combination(self):
        frame = self._frame()
        with (
            mock.patch.object(lib, "load_stack", return_value=[frame]),
            mock.patch.object(
                lib, "die_with_log", side_effect=RuntimeError("bad combo")
            ) as die_with_log,
        ):
            with self.assertRaisesRegex(RuntimeError, "bad combo"):
                next_step.step(
                    "model",
                    {"build_cmd": "true"},
                    False,
                    lib.PIPELINE_CONFIG_FILE,
                    skip_test=True,
                    skip_implementation=True,
                )
        self.assertIn("cannot be combined with --skip-test", die_with_log.call_args.args[1])


class StatusGuidanceTests(unittest.TestCase):
    def test_pending_guidance_mentions_manual_test_path(self):
        frame = lib.CriterionFrame(
            ticket="SA-1",
            criterion="- [ ] do the thing",
            plan_context="ctx",
            test_files=None,
            test_names=None,
            status="pending",
            origin="ticket",
            verification="test",
        )
        printed: list[str] = []
        with (
            mock.patch.object(lib, "load_stack", return_value=[frame]),
            mock.patch(
                "ticket_pipeline.status.render.print_line",
                side_effect=lambda text="": printed.append(text),
            ),
        ):
            status.show_status()
        self.assertTrue(any("--manual-test --manual-test-ref" in line for line in printed))
        self.assertTrue(any("--skip-test" in line for line in printed))

    def test_test_written_guidance_mentions_skip_implementation(self):
        frame = lib.CriterionFrame(
            ticket="SA-1",
            criterion="- [ ] do the thing",
            plan_context="ctx",
            test_files=["tests/test_example.py"],
            test_names=["tests::example"],
            status="test-written",
            origin="ticket",
            verification="test",
        )
        printed: list[str] = []
        with (
            mock.patch.object(lib, "load_stack", return_value=[frame]),
            mock.patch(
                "ticket_pipeline.status.render.print_line",
                side_effect=lambda text="": printed.append(text),
            ),
        ):
            status.show_status()
        self.assertTrue(any("--skip-implementation" in line for line in printed))


class NextStepArgValidationTests(unittest.TestCase):
    def test_manual_test_ref_requires_manual_test_flag(self):
        stderr = io.StringIO()
        with (
            mock.patch.object(
                sys,
                "argv",
                [
                    "next_step",
                    "--manual-test-ref",
                    "tests/test_example.py::tests::example",
                ],
            ),
            mock.patch("sys.stderr", stderr),
        ):
            with self.assertRaises(SystemExit) as cm:
                next_step.main()
        self.assertEqual(2, cm.exception.code)
        self.assertIn("--manual-test-ref requires --manual-test", stderr.getvalue())

    def test_skip_test_rejects_manual_test_combination(self):
        stderr = io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["next_step", "--manual-test", "--skip-test"]),
            mock.patch("sys.stderr", stderr),
        ):
            with self.assertRaises(SystemExit) as cm:
                next_step.main()
        self.assertEqual(2, cm.exception.code)
        self.assertIn("not allowed with argument", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
