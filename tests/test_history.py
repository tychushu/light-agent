import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from termux_agent.history import history_entry
from termux_agent.terminal_ui import Columns


class HistoryTests(unittest.TestCase):
    def test_text_is_readable_and_reasoning_is_not_displayed(self):
        message = {"role": "assistant", "content": "第一段\n\n- 内容", "reasoning_content": "private"}
        self.assertEqual(history_entry(message), ("Agent", "第一段\n\n- 内容"))
        self.assertEqual(history_entry({"role": "user", "content": "hello"}), ("You", "hello"))

    def test_tool_arguments_are_formatted_and_write_bodies_remain_hidden(self):
        message = {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_a", "function": {"name": "write_file", "arguments": json.dumps(
                {"path": "a.txt", "content": "private body"})}}]}
        before = copy.deepcopy(message)
        role, text = history_entry(message)
        self.assertEqual(role, "Agent")
        self.assertIn("Call · write_file · call_a\n{\n", text)
        self.assertIn('"body_hidden": true', text)
        self.assertNotIn("private body", text)
        self.assertIn("private body", history_entry(message, silent_writes=False)[1])
        self.assertEqual(message, before)

    def test_tool_results_render_actual_newlines_and_preserve_call_id(self):
        role, text = history_entry({"role": "tool", "tool_call_id": "call_a",
                                    "content": json.dumps({"stdout": "one\ntwo", "exit_code": 0})})
        self.assertEqual(role, "Tool")
        self.assertIn("Result · call_a", text)
        self.assertIn("stdout:\none\ntwo", text)
        self.assertIn("exit_code:\n0", text)

    def test_empty_malformed_multimodal_and_large_messages(self):
        self.assertEqual(history_entry({"role": "assistant", "content": None})[1], "[empty message]")
        self.assertIn("not json", history_entry({"role": "tool", "content": "not json"})[1])
        self.assertEqual(history_entry({"role": "user", "content": [
            {"type": "text", "text": "hello"}, {"type": "image_url", "image_url": "private"}]})[1],
            "hello\n[image_url]")
        self.assertEqual(history_entry({"role": "user", "content": "x" * 5000})[1],
                         "x" * 4000 + "\n[TRUNCATED]")

    def test_history_uses_unknown_timestamp_and_wraps_in_columns(self):
        out = io.StringIO()
        columns = Columns(out, width=lambda: 60)
        columns.message("Agent", "中文" * 50, stamp="--:--")
        text = out.getvalue()
        self.assertTrue(text.startswith("--:-- Agent"))
        self.assertGreater(len(text.splitlines()), 1)
        self.assertNotIn("\\n", text)

    def test_cli_history_pagination_displays_text_instead_of_message_json(self):
        from termux_agent import cli

        class EchoClient:
            last_usage = last_request = None

            def __init__(self, config):
                pass

            def chat(self, messages, tools):
                return {"role": "assistant", "content": "reply\nsecond line"}

            def close(self):
                pass

        original = Path.cwd()
        out = io.StringIO()
        try:
            with tempfile.TemporaryDirectory() as temp:
                config = Path(temp) / 'config.toml'
                config.write_text('auto_continue = false\n')
                with patch.object(cli.sys, 'argv', ['ta', '--no-session', '--no-agent',
                                  '--config', str(config), '--directory', temp]), \
                        patch.object(cli.sys, 'stdout', out), \
                        patch('builtins.input', side_effect=['one', 'two', '/history 1 1', '/exit']), \
                        patch('termux_agent.agent.Client', EchoClient):
                    cli.main()
        finally:
            os.chdir(original)
        history = out.getvalue().split('历史 3–3 / 4', 1)[1]
        self.assertIn('\ntwo\n', history)
        self.assertNotIn('"role"', history)
        self.assertNotIn('reply', history)
