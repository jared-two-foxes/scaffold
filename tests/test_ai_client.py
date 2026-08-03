"""Tests for run_with_tools' finish_reason handling."""

import unittest
from unittest.mock import patch

from ticket_pipeline.lib.ai_client import AIError, AIResult, run_with_tools


def _mock_response(content, finish_reason, tool_calls=None):
    """Build a minimal chat-completion response shape."""
    message = {"content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "choices": [{"message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }


class TestRunWithToolsFinishReason(unittest.TestCase):
    """finish_reason-aware handling in run_with_tools."""

    def test_length_finish_reason_raises_error(self):
        """A response with finish_reason='length' and no tool_calls
        raises AIError rather than returning a truncated AIResult."""
        mock_resp = _mock_response("truncated mid-sentence", "length")
        with patch(
            "ticket_pipeline.lib.ai_client._post_chat_completion",
            return_value=mock_resp,
        ):
            with self.assertRaises(AIError) as ctx:
                run_with_tools("prompt", [], lambda n, a: "", "test-label")
            self.assertIn("truncated", str(ctx.exception).lower())

    def test_stop_finish_reason_returns_result(self):
        """A normal finish_reason='stop' response with no tool_calls
        returns a completed AIResult with the content and
        finish_reason."""
        mock_resp = _mock_response("complete answer", "stop")
        with patch(
            "ticket_pipeline.lib.ai_client._post_chat_completion",
            return_value=mock_resp,
        ):
            result = run_with_tools("prompt", [], lambda n, a: "", "test-label")
        self.assertIsInstance(result, AIResult)
        self.assertEqual(result.text, "complete answer")
        self.assertEqual(result.finish_reason, "stop")


if __name__ == "__main__":
    unittest.main()
