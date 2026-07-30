import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

render_stub = types.ModuleType("ticket_pipeline.lib.render")
render_stub.render_markdown = lambda _text: None
render_stub.print_line = lambda _text="": None
sys.modules.setdefault("ticket_pipeline.lib.render", render_stub)

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
    def _frame(self, verification="test", status="pending"):
        return lib.CriterionFrame(
            ticket="SA-1",
            criterion="- [ ] thing",
            plan_context="ctx",
            test_files=None,
            test_names=None,
            status=status,
            origin="ticket",
            verification=verification,
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

    def test_manual_only_allows_human(self):
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
            feedback="fix only the failing branch",
            feedback_target=lib.FEEDBACK_TARGET_IMPLEMENTOR,
        )

    def test_tester_feedback_rolls_back_and_rewrites(self):
        frame = self._tester_frame()
        stack = [frame]
        git_cfg = lib.GitConfig(git_workflow=True)
        with mock.patch.object(lib, "git_changed_files", return_value=["tests/test_example.py"]), \
             mock.patch.object(lib, "git_reset_hard") as reset_hard, \
             mock.patch.object(lib, "save_stack"), \
             mock.patch.object(next_step, "do_write_test") as do_write_test:
            next_step._run_feedback_retry(
                stack,
                frame,
                "model",
                {"test_compile_cmd": "true"},
                False,
                3,
                skip_implementation=False,
                continuous=False,
                git_cfg=git_cfg,
            )
        reset_hard.assert_called_once_with("abc123")
        do_write_test.assert_called_once()
        kwargs = do_write_test.call_args.kwargs
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
        with mock.patch.object(lib, "git_changed_files", return_value=["src/example.py"]), \
             mock.patch.object(lib, "save_stack"), \
             mock.patch("ticket_pipeline.implement_step.run_implement_with_refine") as run_refine, \
             mock.patch.object(next_step, "recheck_test_frame") as recheck:
            next_step._run_feedback_retry(
                stack,
                frame,
                "model",
                {"build_cmd": "true"},
                False,
                3,
                skip_implementation=False,
                continuous=False,
                git_cfg=None,
            )
        run_refine.assert_called_once()
        self.assertEqual("fix only the failing branch", run_refine.call_args.kwargs["feedback"])
        self.assertEqual(
            ["src/example.py"], run_refine.call_args.kwargs["previous_changed_files"]
        )
        recheck.assert_called_once()
        self.assertIsNone(frame.feedback)
        self.assertIsNone(frame.feedback_target)
        self.assertEqual(1, frame.feedback_attempts)


if __name__ == "__main__":
    unittest.main()
