import subprocess
import unittest
from pathlib import Path
from unittest import mock

from ticket_pipeline.lib import implement as implement_lib
from ticket_pipeline.lib import pipeline_lib as lib
from ticket_pipeline.lib.retry import FixedBudgetPolicy


class FreshStartRetryTests(unittest.TestCase):
    def _frame(self, *, strategy="tdd", verification="test"):
        return lib.CriterionFrame(
            ticket="SA-1",
            criterion="- [ ] do the thing",
            plan_context="ctx",
            test_files=None,
            test_names=None,
            status="pending",
            origin="ticket",
            verification=verification,
            strategy=strategy,
        )

    def _run_direct_loop(self, *, reset_on_retry: bool, test_commit_sha: str | None, frame=None):
        frame = frame or self._frame()
        prompts = []
        build_results = [
            subprocess.CompletedProcess(args=["build"], returncode=1, stdout="bad", stderr=""),
            subprocess.CompletedProcess(args=["build"], returncode=0, stdout="", stderr=""),
        ]

        def fake_run_with_tools(prompt, tool_list, executor, name, model=None, summarize_call=None):
            prompts.append(prompt)
            executor("write_file", {"path": "src/generated.py", "content": "x"})
            return mock.Mock(text="ok")

        def fake_run_ai_step_with_retry(step_fn, *_args, **_kwargs):
            return step_fn()

        def fake_run_command(command, label, quiet=False):
            return build_results.pop(0)

        with (
            mock.patch.object(implement_lib, "run_with_tools", side_effect=fake_run_with_tools),
            mock.patch.object(
                implement_lib.lib, "run_ai_step_with_retry", side_effect=fake_run_ai_step_with_retry
            ),
            mock.patch.object(implement_lib.lib, "run_command", side_effect=fake_run_command),
            mock.patch.object(implement_lib.lib, "git_reset_hard") as git_reset_hard,
        ):
            changed = implement_lib.run_implement_direct_with_refine(
                frame,
                "model",
                {"build_cmd": "build"},
                2,
                retry_policy=FixedBudgetPolicy(2),
                reset_on_retry=reset_on_retry,
                test_commit_sha=test_commit_sha,
            )
        return changed, prompts, git_reset_hard

    def test_resets_on_retry_when_enabled(self):
        changed, prompts, git_reset_hard = self._run_direct_loop(
            reset_on_retry=True, test_commit_sha="abc123"
        )
        git_reset_hard.assert_called_once_with("abc123")
        self.assertEqual(changed, ["src/generated.py"])
        self.assertEqual(len(prompts), 2)
        self.assertIn("reverted", prompts[1].lower())
        self.assertIn("src/generated.py", prompts[1])

    def test_skips_reset_when_disabled(self):
        _changed, _prompts, git_reset_hard = self._run_direct_loop(
            reset_on_retry=False, test_commit_sha="abc123"
        )
        git_reset_hard.assert_not_called()

    def test_skips_reset_without_test_commit_sha(self):
        _changed, _prompts, git_reset_hard = self._run_direct_loop(
            reset_on_retry=True, test_commit_sha=None
        )
        git_reset_hard.assert_not_called()

    def test_direct_loop_accepts_empty_write_when_build_passes(self):
        frame = self._frame()
        prompts = []

        def fake_run_with_tools(prompt, _tool_list, _executor, _name, **_kwargs):
            prompts.append(prompt)
            return mock.Mock(text="ok")

        with (
            mock.patch.object(implement_lib, "run_with_tools", side_effect=fake_run_with_tools),
            mock.patch.object(
                implement_lib.lib,
                "run_ai_step_with_retry",
                side_effect=lambda step_fn, *_a, **_k: step_fn(),
            ),
            mock.patch.object(
                implement_lib.lib, "die_with_log", side_effect=RuntimeError("should not die")
            ),
            mock.patch.object(
                implement_lib.lib, "extract_referenced_paths", return_value=["src/lib.py"]
            ),
            mock.patch.object(implement_lib.lib, "git_changed_files", return_value=["src/lib.py"]),
        ):
            changed = implement_lib.run_implement_direct_with_refine(
                frame, "model", {"build_cmd": "build"}, 2, retry_policy=FixedBudgetPolicy(2)
            )
        self.assertEqual(changed, [])
        self.assertEqual(len(prompts), 1)

    def test_with_refine_retries_when_scoped_tests_stay_red(self):
        frame = lib.CriterionFrame(
            ticket="SA-1",
            criterion="- [ ] do the thing",
            plan_context="ctx",
            test_files=["tests/test_example.py"],
            test_names=["tests.test_example"],
            status="pending",
            origin="ticket",
        )
        prompts = []
        build_results = [
            subprocess.CompletedProcess(args=["build"], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=["build"], returncode=0, stdout="", stderr=""),
        ]
        green_results = [
            [subprocess.CompletedProcess(args=["test"], returncode=1, stdout="fail", stderr="")],
            [subprocess.CompletedProcess(args=["test"], returncode=0, stdout="", stderr="")],
        ]

        def fake_run_with_tools(prompt, _tool_list, executor, _name, **_kwargs):
            prompts.append(prompt)
            executor("write_file", {"path": "src/generated.py", "content": "x"})
            return mock.Mock(text="ok")

        with (
            mock.patch.object(implement_lib, "run_with_tools", side_effect=fake_run_with_tools),
            mock.patch.object(
                implement_lib.lib,
                "run_ai_step_with_retry",
                side_effect=lambda step_fn, *_a, **_k: step_fn(),
            ),
            mock.patch.object(
                implement_lib.lib, "run_command", side_effect=lambda *_a, **_k: build_results.pop(0)
            ),
            mock.patch.object(
                implement_lib.lib,
                "run_scoped_tests",
                side_effect=lambda *_a, **_k: green_results.pop(0),
            ),
            mock.patch.object(implement_lib, "verify_tests_unchanged"),
            mock.patch.object(
                implement_lib.lib, "die_with_log", side_effect=RuntimeError("should not die")
            ),
        ):
            changed = implement_lib.run_implement_with_refine(
                frame,
                "model",
                {"build_cmd": "build", "test_filter_cmd": "pytest -q {filter}"},
                2,
                retry_policy=FixedBudgetPolicy(2),
            )
        self.assertEqual(changed, ["src/generated.py"])
        self.assertEqual(len(prompts), 2)
        self.assertEqual(changed, ["src/generated.py"])
        self.assertEqual(len(prompts), 2)

    def test_direct_strategy_and_skip_test_use_the_new_prompt(self):
        direct_changed, direct_prompts, _ = self._run_direct_loop(
            reset_on_retry=False,
            test_commit_sha=None,
            frame=self._frame(strategy="direct", verification="manual"),
        )
        skip_changed, skip_prompts, _ = self._run_direct_loop(
            reset_on_retry=False,
            test_commit_sha=None,
            frame=self._frame(strategy="tdd", verification="manual"),
        )
        self.assertEqual(["src/generated.py"], direct_changed)
        self.assertEqual(["src/generated.py"], skip_changed)
        self.assertEqual(direct_prompts, skip_prompts)
        self.assertNotIn("already judged untestable", direct_prompts[0].lower())
        self.assertNotIn("do not second-guess it", direct_prompts[0].lower())
        self.assertIn(
            "make the change one specific acceptance criterion describes",
            " ".join(direct_prompts[0].split()),
        )

    def test_unrelated_generated_file_is_removed_or_justified(self):
        repository_root = Path(__file__).resolve().parents[1]
        generated_file = repository_root / "src" / "generated.py"
        if not generated_file.exists():
            return

        plan_file = repository_root / ".scaffold" / ".implementation-plan.md"
        plan_text = plan_file.read_text(encoding="utf-8")
        plan_section = plan_text.split("## Implementation Plan", 1)[-1].split("## ", 1)[0]
        justification_lines = [
            line.strip()
            for line in plan_section.splitlines()
            if line.strip().startswith("-") and "src/generated.py" in line
        ]
        self.assertTrue(
            justification_lines,
            "src/generated.py must be removed or explicitly justified in the Implementation Plan",
        )
        for line in justification_lines:
            self.assertTrue(line.split("src/generated.py", 1)[-1].strip(" `:-—").strip())


if __name__ == "__main__":
    unittest.main()
