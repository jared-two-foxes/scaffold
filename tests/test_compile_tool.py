import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ticket_pipeline.lib import pipeline_lib as lib
from ticket_pipeline.lib import tools


class CompileToolTests(unittest.TestCase):
    def test_compile_disabled_returns_error(self):
        executor = tools.make_executor(allow_compile=False)
        result = executor("compile", {})
        self.assertIn("not enabled", result)

    def test_compile_enabled_runs_command(self):
        with mock.patch.object(
            lib,
            "run_command",
            return_value=mock.Mock(returncode=0, stdout="", stderr=""),
        ) as run_command:
            executor = tools.make_executor(allow_compile=True, compile_cmd="echo hello")
            result = executor("compile", {})
        self.assertIn("successful", result.lower())
        run_command.assert_called_once()

    def test_compile_failure_returns_output(self):
        with mock.patch.object(
            lib,
            "run_command",
            return_value=mock.Mock(returncode=1, stdout="error", stderr=""),
        ):
            executor = tools.make_executor(allow_compile=True, compile_cmd="false")
            result = executor("compile", {})
        self.assertIn("failed", result.lower())
        self.assertIn("error", result)

    def test_compile_limit_per_turn(self):
        with mock.patch.object(
            lib,
            "run_command",
            return_value=mock.Mock(returncode=0, stdout="", stderr=""),
        ):
            executor = tools.make_executor(
                allow_compile=True,
                compile_cmd="true",
                max_compiles_per_turn=2,
            )
            executor("compile", {})
            executor("compile", {})
            result = executor("compile", {})
        self.assertIn("limit", result.lower())

    def test_summarize_compile_call(self):
        self.assertEqual(tools.summarize_tool_call("compile", {}), "Compile project")

    def test_edit_file_replaces_unique_match_and_invalidates_cache(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_cwd = os.getcwd()
            os.chdir(tmpdir)
            try:
                path = Path("sample.txt")
                path.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
                written_paths = []
                executor = tools.make_executor(written_paths=written_paths)

                first = executor("read_file", {"path": "sample.txt"})
                self.assertIn("beta", first)

                result = executor(
                    "edit_file",
                    {"path": "sample.txt", "old_text": "beta\n", "new_text": "BETA\n"},
                )

                self.assertIn("edited", result.lower())
                self.assertEqual(path.read_text(encoding="utf-8"), "alpha\nBETA\ngamma\n")
                self.assertEqual(written_paths, ["sample.txt"])

                second = executor("read_file", {"path": "sample.txt"})
                self.assertIn("BETA", second)
                self.assertNotIn("duplicate read_file", second)
            finally:
                os.chdir(original_cwd)

    def test_edit_file_rejects_missing_or_ambiguous_targets(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_cwd = os.getcwd()
            os.chdir(tmpdir)
            try:
                path = Path("sample.txt")
                path.write_text("alpha\nalpha\n", encoding="utf-8")
                executor = tools.make_executor()

                missing = executor(
                    "edit_file",
                    {"path": "sample.txt", "old_text": "beta", "new_text": "BETA"},
                )
                self.assertIn("ERROR", missing)
                self.assertIn("not found", missing.lower())

                ambiguous = executor(
                    "edit_file",
                    {"path": "sample.txt", "old_text": "alpha", "new_text": "BETA"},
                )
                self.assertIn("ERROR", ambiguous)
                self.assertIn("multiple", ambiguous.lower())
            finally:
                os.chdir(original_cwd)

    def test_edit_file_refuses_protected_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_cwd = os.getcwd()
            os.chdir(tmpdir)
            try:
                path = Path("sample.txt")
                path.write_text("alpha\n", encoding="utf-8")
                executor = tools.make_executor(protected_paths={"sample.txt"})

                result = executor(
                    "edit_file",
                    {"path": "sample.txt", "old_text": "alpha", "new_text": "BETA"},
                )

                self.assertIn("ERROR", result)
                self.assertIn("protected", result.lower())
                self.assertEqual(path.read_text(encoding="utf-8"), "alpha\n")
            finally:
                os.chdir(original_cwd)
