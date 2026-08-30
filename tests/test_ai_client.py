"""Tests for run_with_tools and the Responses protocol fallback."""

import json
import unittest
from unittest.mock import patch

from ticket_pipeline.lib import ai_client
from ticket_pipeline.lib.ai_client import (
    AIError,
    AIHTTPError,
    AIResult,
    _post,
    run_prompt,
    run_with_tools,
)


def _mock_chat_response(content, finish_reason, tool_calls=None):
    """Build a minimal chat-completions response body."""
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "choices": [{"message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }


def _mock_responses_response(output_items, usage=None, status="completed"):
    """Build a raw opencode zen Responses API response body."""
    return {
        "status": status,
        "output": output_items,
        "usage": usage or {},
    }


class TestRunWithToolsFinishReason(unittest.TestCase):
    """finish_reason-aware handling in run_with_tools."""

    def test_length_finish_reason_raises_error(self):
        """A response with finish_reason='length' and no tool_calls
        raises AIError rather than returning a truncated AIResult."""
        mock_resp = _mock_chat_response("truncated mid-sentence", "length")
        with patch(
            "ticket_pipeline.lib.ai_client._post",
            return_value=mock_resp,
        ):
            with self.assertRaises(AIError) as ctx:
                run_with_tools("prompt", [], lambda n, a: "", "test-label")
            self.assertIn("truncated", str(ctx.exception).lower())

    def test_stop_finish_reason_returns_result(self):
        """A normal finish_reason='stop' response with no tool_calls
        returns a completed AIResult with the content and
        finish_reason."""
        mock_resp = _mock_chat_response("complete answer", "stop")
        with patch(
            "ticket_pipeline.lib.ai_client._post",
            return_value=mock_resp,
        ):
            result = run_with_tools("prompt", [], lambda n, a: "", "test-label")
        self.assertIsInstance(result, AIResult)
        self.assertEqual(result.text, "complete answer")
        self.assertEqual(result.finish_reason, "stop")


class TestResponsesTruncation(unittest.TestCase):
    """Responses API truncation handling."""

    def test_incomplete_max_output_tokens_raises_error(self):
        """A Responses response with status='incomplete' and reason
        'max_output_tokens' raises an AIError whose message contains
        'truncated by length limit', matching the chat-completions
        finish_reason='length' behavior."""
        raw_resp = {
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [],
            "usage": {"input_tokens": 10, "output_tokens": 20},
        }

        def fake_post(path, payload, label, provider):
            if path == "chat/completions":
                raise AIHTTPError(500, "chat/completions unavailable")
            return raw_resp

        with patch("ticket_pipeline.lib.ai_client._RESPONSES_PROTOCOL_CACHE", {}):
            with patch(
                "ticket_pipeline.lib.ai_client._post",
                side_effect=fake_post,
            ):
                with self.assertRaises(AIError) as ctx:
                    run_with_tools("prompt", [], lambda n, a: "", "test-label")

        self.assertIn("truncated by length limit", str(ctx.exception).lower())


class TestResponsesToolLoop(unittest.TestCase):
    """Multi-turn tool loop over the opencode zen Responses protocol."""

    def test_responses_function_call_loop(self):
        """A function_call item in the Responses output[] is dispatched to
        executor, the result is fed back as a function_call_output with the
        same call_id, and the subsequent response yields the final text.
        """
        responses = [
            _mock_responses_response(
                [
                    {
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "lookup",
                        "arguments": json.dumps({"q": "x"}),
                    }
                ],
                usage={"input_tokens": 10, "output_tokens": 5},
            ),
            _mock_responses_response(
                [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "answer: 42"}],
                    }
                ],
                usage={"input_tokens": 20, "output_tokens": 5},
            ),
        ]

        calls = []
        executor_calls = []

        def fake_post(path, payload, label, provider):
            calls.append((path, payload, label))
            if path == "chat/completions":
                raise AIHTTPError(500, "chat/completions unavailable")
            response_index = sum(1 for p, _, _ in calls if p == "responses") - 1
            return responses[response_index]

        def executor(name, args):
            executor_calls.append((name, args))
            return f"{name}({args})"

        with patch("ticket_pipeline.lib.ai_client._RESPONSES_PROTOCOL_CACHE", {}):
            with patch("ticket_pipeline.lib.ai_client._post", side_effect=fake_post):
                result = run_with_tools(
                    "prompt",
                    [
                        {
                            "type": "function",
                            "function": {
                                "name": "lookup",
                                "description": "Look something up.",
                                "parameters": {"type": "object"},
                            },
                        }
                    ],
                    executor,
                    "test-label",
                )

        self.assertIsInstance(result, AIResult)
        self.assertEqual(result.text, "answer: 42")
        self.assertEqual(result.finish_reason, "stop")

        self.assertEqual(executor_calls, [("lookup", {"q": "x"})])

        self.assertEqual(
            [path for path, _, _ in calls],
            ["chat/completions", "responses", "responses"],
        )

        response_calls = [c for c in calls if c[0] == "responses"]
        self.assertEqual(len(response_calls), 2)

        first_payload = response_calls[0][1]
        self.assertEqual(
            first_payload["input"],
            [{"role": "user", "content": [{"type": "input_text", "text": "prompt"}]}],
        )

        second_payload = response_calls[1][1]
        self.assertEqual(
            second_payload["input"],
            [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "prompt"}],
                },
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "lookup",
                    "arguments": json.dumps({"q": "x"}),
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": "lookup({'q': 'x'})",
                },
            ],
        )


class TestResponsesProtocolCache(unittest.TestCase):
    """Per-model cache of models that require the Responses protocol."""

    def _text_item(self, text: str) -> dict:
        return {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        }

    def test_subsequent_requests_use_responses_directly(self):
        """After a model 500s on chat-completions and succeeds on responses,
        later requests for the same model id go straight to responses
        without re-probing chat-completions.
        """
        responses = iter(
            [
                _mock_responses_response(
                    [self._text_item("first")],
                    usage={"input_tokens": 1, "output_tokens": 1},
                ),
                _mock_responses_response(
                    [self._text_item("second")],
                    usage={"input_tokens": 1, "output_tokens": 1},
                ),
            ]
        )
        calls = []

        def fake_post(path, payload, label, provider):
            calls.append((path, payload["model"]))
            if path == "chat/completions":
                raise AIHTTPError(500, "chat/completions unavailable")
            return next(responses)

        with patch("ticket_pipeline.lib.ai_client._RESPONSES_PROTOCOL_CACHE", {}):
            with patch("ticket_pipeline.lib.ai_client._post", side_effect=fake_post):
                r1 = run_with_tools(
                    "prompt1",
                    [],
                    lambda n, a: "",
                    "test-label",
                    model="opencode:model-a",
                )
                r2 = run_with_tools(
                    "prompt2",
                    [],
                    lambda n, a: "",
                    "test-label",
                    model="opencode:model-a",
                )

        self.assertEqual(r1.text, "first")
        self.assertEqual(r2.text, "second")
        # First call probes chat-completions then falls back; second call
        # uses the cached Responses protocol directly.
        self.assertEqual(
            calls,
            [
                ("chat/completions", "model-a"),
                ("responses", "model-a"),
                ("responses", "model-a"),
            ],
        )

    def test_cache_is_per_model_id(self):
        """A cached model id does not cause a different model id to skip
        the chat-completions probe.
        """
        calls = []

        def fake_post(path, payload, label, provider):
            calls.append((path, payload["model"]))
            if path == "chat/completions":
                raise AIHTTPError(500, "chat/completions unavailable")
            return _mock_responses_response(
                [self._text_item("ok")],
                usage={"input_tokens": 1, "output_tokens": 1},
            )

        with patch("ticket_pipeline.lib.ai_client._RESPONSES_PROTOCOL_CACHE", {}):
            with patch("ticket_pipeline.lib.ai_client._post", side_effect=fake_post):
                run_with_tools(
                    "prompt",
                    [],
                    lambda n, a: "",
                    "test-label",
                    model="opencode:model-a",
                )
                run_with_tools(
                    "prompt",
                    [],
                    lambda n, a: "",
                    "test-label",
                    model="opencode:model-b",
                )

        # model-a is seeded, but model-b must still be probed on chat-completions
        # because the cache is keyed per model id.
        self.assertIn(("chat/completions", "model-b"), calls)
        chat_completions_calls = [c for c in calls if c[0] == "chat/completions"]
        self.assertEqual(len(chat_completions_calls), 2)

    def test_run_prompt_uses_responses_cache(self):
        """run_prompt also benefits from the per-model Responses cache."""
        responses = iter(
            [
                _mock_responses_response(
                    [self._text_item("prompt one")],
                    usage={"input_tokens": 1, "output_tokens": 1},
                ),
                _mock_responses_response(
                    [self._text_item("prompt two")],
                    usage={"input_tokens": 1, "output_tokens": 1},
                ),
            ]
        )
        calls = []

        def fake_post(path, payload, label, provider):
            calls.append((path, payload["model"]))
            if path == "chat/completions":
                raise AIHTTPError(500, "chat/completions unavailable")
            return next(responses)

        with patch("ticket_pipeline.lib.ai_client._RESPONSES_PROTOCOL_CACHE", {}):
            with patch("ticket_pipeline.lib.ai_client._post", side_effect=fake_post):
                r1 = run_prompt("prompt1", "test-label", model="opencode:model-c")
                r2 = run_prompt("prompt2", "test-label", model="opencode:model-c")

        self.assertEqual(r1.text, "prompt one")
        self.assertEqual(r2.text, "prompt two")
        self.assertEqual(
            calls,
            [
                ("chat/completions", "model-c"),
                ("responses", "model-c"),
                ("responses", "model-c"),
            ],
        )


class TestResponsesUsageAccumulation(unittest.TestCase):
    """Responses API usage is accumulated into ai_client.usage."""

    def setUp(self):
        super().setUp()
        ai_client.usage = ai_client.UsageTracker()

    def test_responses_usage_maps_to_model_usage(self):
        """A successful Responses response with input_tokens/output_tokens
        populates the per-model UsageTracker as prompt_tokens/completion_tokens.
        """
        model = "opencode:responses-model"

        def fake_post(path, payload, label, provider):
            if path == "chat/completions":
                raise AIHTTPError(500, "chat/completions unavailable")
            return _mock_responses_response(
                [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "ok"}],
                    }
                ],
                usage={"input_tokens": 7, "output_tokens": 3},
            )

        with patch("ticket_pipeline.lib.ai_client._RESPONSES_PROTOCOL_CACHE", {}):
            with patch(
                "ticket_pipeline.lib.ai_client._post",
                side_effect=fake_post,
            ):
                result = run_with_tools(
                    "prompt",
                    [],
                    lambda n, a: "",
                    "test-label",
                    model=model,
                )

        self.assertEqual(result.text, "ok")
        self.assertEqual(result.finish_reason, "stop")
        self.assertIn(model, ai_client.usage.by_model)
        self.assertEqual(ai_client.usage.by_model[model].prompt_tokens, 7)
        self.assertEqual(ai_client.usage.by_model[model].completion_tokens, 3)


if __name__ == "__main__":
    unittest.main()
