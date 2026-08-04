import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ticket_pipeline import give_feedback, next_step
from ticket_pipeline.lib import pipeline_lib as lib


class _chdir:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.prev = None

    def __enter__(self):
        import os

        self.prev = os.getcwd()
        os.chdir(self.path)

    def __exit__(self, *exc):
        import os

        os.chdir(self.prev)


class ResolveFeedbackTargetTests(unittest.TestCase):
    def _frame(self, verification="test", status="pending", strategy=None):
        strategy = strategy or {
            "manual": "direct",
            "refactor": "refactor",
        }.get(verification, "tdd")
        return lib.CriterionFrame(
            ticket="SA-1",
            criterion="- [ ] thing",
            plan_context="ctx",
            test_files=None,
            test_names=None,
            status=status,
            origin="ticket",
            verification=verification,
            strategy=strategy,
        )

    def test_test_refactor_defaults_to_tester(self):
        self.assertEqual(
            lib.FEEDBACK_TARGET_TESTER,
            lib.resolve_feedback_target(self._frame(verification="test-refactor"), "auto"),
        )

    def test_refactor_defaults_to_implementor(self):
        self.assertEqual(
            lib.FEEDBACK_TARGET_IMPLEMENTOR,
            lib.resolve_feedback_target(self._frame(verification="refactor"), "auto"),
        )

    def test_manual_defaults_to_implementor(self):
        self.assertEqual(
            lib.FEEDBACK_TARGET_IMPLEMENTOR,
            lib.resolve_feedback_target(self._frame(verification="manual"), "auto"),
        )

    def test_manual_rejects_tester_target(self):
        with self.assertRaises(ValueError):
            lib.resolve_feedback_target(self._frame(verification="manual"), "tester")


class GiveFeedbackCommandTests(unittest.TestCase):
    def _frame(self):
        return lib.CriterionFrame(
            ticket="SA-1",
            criterion="- [ ] refactor test",
            plan_context="ctx",
            test_files=["tests/test_example.py"],
            test_names=["tests::example"],
            status="test-written",
            origin="ticket",
            verification="test-refactor",
            base_commit="abc123",
        )

    def test_queues_feedback_on_top_frame(self):
        with tempfile.TemporaryDirectory() as d, _chdir(Path(d)):
            lib.save_stack([self._frame()])
            cfg = Path(d) / "cfg.toml"
            cfg.write_text("git_workflow = true\n", encoding="utf-8")
            argv = [
                "give-feedback",
                "--config",
                str(cfg),
                "--target",
                "tester",
                "too",
                "broad",
            ]
            with mock.patch.object(sys, "argv", argv):
                give_feedback.main()
            [frame] = lib.load_stack()
        self.assertEqual(lib.FEEDBACK_READY_STATUS, frame.status)
        self.assertEqual(lib.FEEDBACK_TARGET_TESTER, frame.feedback_target)
        self.assertEqual("too broad", frame.feedback)


class FeedbackRetryTests(unittest.TestCase):
    def _tester_frame(self):
        return lib.CriterionFrame(
            ticket="SA-1",
            criterion="- [ ] refactor test",
            plan_context="ctx",
            test_files=["tests/test_example.py"],
            test_names=["tests::example"],
            status=lib.FEEDBACK_READY_STATUS,
            origin="ticket",
            verification="test-refactor",
            base_commit="abc123",
            feedback="narrow the rewrite",
            feedback_target=lib.FEEDBACK_TARGET_TESTER,
        )

    def _implementor_frame(self):
        return lib.CriterionFrame(
            ticket="SA-2",
            criterion="- [ ] implement thing",
            plan_context="ctx",
            test_files=["tests/test_example.py"],
            test_names=["tests::example"],
            status=lib.FEEDBACK_READY_STATUS,
            origin="ticket",
            verification="test",
            strategy="tdd",
            feedback="fix only the failing branch",
            feedback_target=lib.FEEDBACK_TARGET_IMPLEMENTOR,
        )

    def test_tester_feedback_rolls_back_and_rewrites(self):
        frame = self._tester_frame()
        stack = [frame]
        git_cfg = lib.GitConfig(git_workflow=True)
        ctx = lib.StepContext(
            model="model",
            step_models={},
            commands={"test_compile_cmd": "true"},
            config_path=lib.PIPELINE_CONFIG_FILE,
            continuous=False,
            max_attempts=3,
            accept_no_test=False,
            skip_implementation=False,
            git_cfg=git_cfg,
        )
        with (
            mock.patch.object(lib, "git_changed_files", return_value=["tests/test_example.py"]),
            mock.patch.object(lib, "git_reset_hard") as reset_hard,
            mock.patch.object(lib, "save_stack"),
            mock.patch("ticket_pipeline.strategies.tdd.do_write_test") as do_write_test,
        ):
            next_step._run_feedback_retry(stack, frame, ctx)
        reset_hard.assert_called_once_with("abc123")
        do_write_test.assert_called_once()
        call_args = do_write_test.call_args
        self.assertIs(stack, call_args.args[0])
        self.assertIs(frame, call_args.args[1])
        self.assertIs(ctx, call_args.args[2])
        kwargs = call_args.kwargs
        self.assertEqual("narrow the rewrite", kwargs["feedback"])
        self.assertEqual(["tests/test_example.py"], kwargs["previous_changed_files"])
        self.assertIsNone(frame.feedback)
        self.assertIsNone(frame.feedback_target)
        self.assertEqual("pending", frame.status)
        self.assertIsNone(frame.base_commit)
        self.assertEqual(1, frame.feedback_attempts)

    def test_implementor_feedback_uses_refine_path(self):
        frame = self._implementor_frame()
        stack = [frame]
        seen_statuses = []
        ctx = lib.StepContext(
            model="model",
            step_models={},
            commands={"build_cmd": "true"},
            config_path=lib.PIPELINE_CONFIG_FILE,
            continuous=False,
            max_attempts=3,
            accept_green=False,
            accept_manual=False,
            accept_no_test=False,
            skip_implementation=False,
            git_cfg=None,
        )

        def _record_status(*args, **kwargs):
            seen_statuses.append(frame.status)

        with (
            mock.patch.object(lib, "git_changed_files", return_value=["src/example.py"]),
            mock.patch.object(lib, "save_stack"),
            mock.patch(
                "ticket_pipeline.strategies.tdd.implement", side_effect=_record_status
            ) as implement,
            mock.patch("ticket_pipeline.strategies.tdd.recheck") as recheck,
        ):
            next_step._run_feedback_retry(stack, frame, ctx)
        implement.assert_called_once()
        self.assertEqual(["test-written"], seen_statuses)
        self.assertEqual("fix only the failing branch", implement.call_args.kwargs["feedback"])
        self.assertEqual(["src/example.py"], implement.call_args.kwargs["previous_changed_files"])
        impl_ctx = implement.call_args.args[1]
        self.assertFalse(impl_ctx.accept_green)
        self.assertFalse(impl_ctx.accept_manual)
        recheck.assert_called_once()
        self.assertIsNone(frame.feedback)
        self.assertIsNone(frame.feedback_target)
        self.assertEqual(1, frame.feedback_attempts)


if __name__ == "__main__":
    unittest.main()
