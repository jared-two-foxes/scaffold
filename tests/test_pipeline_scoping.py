import unittest
from unittest import mock

from ticket_pipeline.lib import pipeline_lib as lib


class ScopedTestCommandTests(unittest.TestCase):
    def test_integration_test_target_is_included_in_cargo_test_command(self):
        commands = {"test_filter_cmd": "cargo test {test_target} {filter}"}
        completed = mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(lib, "run_command", return_value=completed) as run_command:
            lib.run_scoped_test(
                "crate::checks::rejects_invalid_input",
                commands,
                "scoped test",
                test_target="--test checks",
            )

        run_command.assert_called_once_with(
            "cargo test --test checks crate::checks::rejects_invalid_input",
            "scoped test",
            quiet=False,
        )


if __name__ == "__main__":
    unittest.main()
