import unittest
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
