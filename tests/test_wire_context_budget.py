"""Request-aware character and prompt-token budget projections."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from termux_agent.agent import Agent
from termux_agent.client import Client
from termux_agent.config import Config
from termux_agent.context import context_chars
from termux_agent.token_budget import TokenCounter


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class WireContextBudgetTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.home_patch = patch.dict(os.environ, {"HOME": self.home.name})
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.addCleanup(self.home.cleanup)

    def client(self, wire_api="responses", **changes):
        config = Config(base_url="http://127.0.0.1:9/v1", model="wire-test", stream=False,
                        wire_api=wire_api, **changes)
        http = httpx.Client(transport=httpx.MockTransport(
            lambda request: self.fail("budget projection must not send HTTP")))
        self.addCleanup(http.close)
        return Client(config, http_client=http)

    def test_responses_projection_deduplicates_display_and_keeps_reasoning_tool_pairing(self):
        encrypted = "O" * 256
        messages = [
            {"role": "user", "content": "inspect"},
            {"role": "assistant", "content": "shared display 中文", "reasoning_content": "local display",
             "tool_calls": [{"id": "call-1", "type": "function",
                             "function": {"name": "read_file", "arguments": {"path": "x"}}}],
             "_responses_output": [
                 {"type": "message", "role": "assistant",
                  "content": [{"type": "output_text", "text": "shared display 中文"}]},
                 {"type": "reasoning", "summary": [], "encrypted_content": encrypted},
                 {"type": "function_call", "call_id": "call-1", "name": "read_file",
                  "arguments": '{"path":"x"}'},
             ]},
            {"role": "tool", "tool_call_id": "call-1", "content": '{"ok":true}'},
        ]
        client = self.client()
        _, body = client.prepare_request(messages, [])
        context = client.request_context(messages, [])

        self.assertEqual(context["chars"], len(compact(body)))
        self.assertLess(context["chars"], len(compact(body).encode("utf-8")))
        self.assertEqual(context["prompt_wire"].count("shared display"), 1)
        self.assertNotIn("local display", context["prompt_wire"])
        self.assertIn(encrypted, context["prompt_wire"])
        counter = TokenCounter(client.config, cache_path=Path(self.home.name) / "tokens.json")
        with patch("termux_agent.token_budget.estimate_text_tokens", return_value=19) as estimate:
            token = counter.measure_request(context, allow_network=False)
        estimate.assert_called_once_with(context["prompt_wire"])
        self.assertEqual(token["prompt_tokens"], 19 + 32 + 6 * len(context["messages"]))
        items = body["input"]
        call_index = next(i for i, item in enumerate(items) if item["type"] == "function_call")
        self.assertEqual(items[call_index + 1]["type"], "function_call_output")
        self.assertEqual(items[call_index + 1]["call_id"], "call-1")

    def test_chat_and_responses_budgets_use_the_same_body_as_chat(self):
        messages = [
            {"role": "system", "content": "rules"},
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "working", "reasoning_content": "display-only",
             "tool_calls": [{"id": "call-1", "type": "function",
                             "function": {"name": "read_file", "arguments": {"path": "x"}}}],
             "_responses_output": [{"type": "reasoning", "encrypted_content": "encrypted-state"}]},
            {"role": "tool", "tool_call_id": "call-1", "content": "ok"},
        ]
        tools = [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}]
        for wire_api in ("chat_completions", "responses"):
            with self.subTest(wire_api=wire_api):
                client = self.client(wire_api)
                _, body = client.prepare_request(messages, tools)
                expected = compact(body).encode("utf-8")

                def send(wire, *_args):
                    self.assertEqual(wire, expected)
                    return {"content": "ok"}

                with patch.object(client, "_chat_attempts", side_effect=send):
                    client.chat(messages, tools)
                context = client.request_context(messages, tools)
                self.assertEqual(context["chars"], len(expected.decode("utf-8")))
                self.assertEqual(context["provider_prompt_tokens"], None)
                if wire_api == "chat_completions":
                    self.assertIn("display-only", context["prompt_wire"])
                    self.assertNotIn("encrypted-state", context["prompt_wire"])
                    self.assertIn('"tool_call_id":"call-1"', context["prompt_wire"])
                else:
                    self.assertNotIn("display-only", context["prompt_wire"])
                    self.assertIn("encrypted-state", context["prompt_wire"])
                    self.assertIn('"call_id":"call-1"', context["prompt_wire"])

    def test_provider_usage_is_reused_only_for_same_body_and_profile(self):
        messages = [{"role": "user", "content": "same"}]
        client = self.client()

        def send(_wire, *_args):
            client.last_usage = {"prompt_tokens": 37, "completion_tokens": 1}
            return {"content": "ok"}

        with patch.object(client, "_chat_attempts", side_effect=send):
            client.chat(messages, [])
        self.assertEqual(client.request_context(messages, [])["provider_prompt_tokens"], 37)
        self.assertIsNone(client.request_context([{"role": "user", "content": "other"}], [])["provider_prompt_tokens"])
        client.config.base_url = "http://127.0.0.1:10/v1"
        self.assertIsNone(client.request_context(messages, [])["provider_prompt_tokens"])

    def test_client_subclass_and_custom_mock_keep_legacy_budget_path(self):
        class Adapter(Client):
            def chat(self, *_args):
                raise AssertionError("not used")

        config = Config(token_count_mode="off")
        for client in (Adapter(config, http_client=httpx.Client()), type("MockClient", (), {})()):
            self.addCleanup(getattr(client, "close", lambda: None))
            agent = Agent(config, client=client)
            request = [{"role": "user", "content": "hello"}]
            chars, token, exceeded = agent._context_budget(request, allow_network=False)
            self.assertEqual(chars, context_chars(request, agent.tools))
            self.assertIsNone(token["prompt_tokens"])
            self.assertFalse(exceeded)


if __name__ == "__main__":
    unittest.main()
