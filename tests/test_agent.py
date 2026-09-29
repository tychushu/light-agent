"""Focused stdlib tests for Termux Agent tools, safety and loop behavior."""
import json
import tempfile
import time
import unittest
from pathlib import Path

from termux_agent import tools
from termux_agent.approval import needs_approval
from termux_agent.config import Config
from termux_agent.agent import Agent
from termux_agent.client import Client, ClientError

try:
    import httpx
except ImportError:
    httpx = None


class ToolTests(unittest.TestCase):
    def test_line_windows_are_inclusive_bounded_and_default_to_first_50(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "lines"
            path.write_text("".join(f"row {i}\n" for i in range(1, 102)))
            result = tools.execute("read_file", {"path": str(path), "start_line": 10, "end_line": 12})
            self.assertEqual(result["content"], "10: row 10\n11: row 11\n12: row 12\n")
            self.assertEqual(result["next_line"], 13)
            default = tools.execute("read_file", {"path": str(path)})
            self.assertIn("50: row 50", default["content"])
            self.assertNotIn("51: row 51", default["content"])
            self.assertFalse(default["truncated"])

    def test_line_windows_truncate_huge_lines_and_reject_mixed_offsets(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "huge"
            path.write_text("A" * 100000 + "\nlast\n")
            result = tools.execute("read_file", {"path": str(path), "start_line": 1, "end_line": 2},
                                   max_output_chars=300)
            self.assertLessEqual(len(result["content"]), 300)
            self.assertIn("[LINE TRUNCATED]", result["content"])
            self.assertIn("next_line", result)
            self.assertIn("error", tools.execute("read_file", {"path": str(path), "start_line": 1, "offset": 0}))

    def test_file_round_trip_and_edit_mismatch_preserves_file(self):
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "note.txt")
            self.assertNotIn("error", tools.execute("write_file", {"path": path, "content": "one two one"}))
            result = tools.execute("edit_file", {"path": path, "old_text": "one", "new_text": "ONE", "expected_replacements": 2})
            self.assertEqual(result["replacements"], 2)
            before = Path(path).read_text()
            result = tools.execute("edit_file", {"path": path, "old_text": "one", "new_text": "x", "expected_replacements": 1})
            self.assertIn("error", result)
            self.assertEqual(Path(path).read_text(), before)
            read = tools.execute("read_file", {"path": path})
            self.assertEqual(read["content"], "1: ONE two ONE")

    def test_write_file_creates_nested_parent_directories(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "new" / "nested" / "file.txt"
            result = tools.execute("write_file", {"path": str(path), "content": "hello"})
            self.assertEqual(result["bytes_written"], 5)
            self.assertEqual(path.read_text(), "hello")

    def test_shell_waits_after_output_pipes_close(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "completed"
            result = tools.execute("shell", {"command": f"exec >/dev/null 2>&1; sleep 0.1; touch {path}", "timeout": 2})
            self.assertEqual(result["exit_code"], 0)
            self.assertTrue(path.exists())

    def test_default_read_keeps_real_head_and_tail(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "large"
            path.write_text("HEAD" + "x" * 10000 + "TAIL")
            result = tools.execute("read_file", {"path": str(path)}, max_output_chars=200)
            self.assertTrue(result["truncated"])
            self.assertTrue(result["content"].startswith("1: HEAD"))
            self.assertIn("TAIL", result["content"])
            self.assertLessEqual(len(result["content"]), 200)

    def test_read_error_is_a_tool_result(self):
        with tempfile.TemporaryDirectory() as td:
            result = tools.execute("read_file", {"path": str(Path(td) / "missing")})
            self.assertIn("error", result)

    def test_read_window_is_bounded(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "large.txt"
            path.write_text("0123456789" * 1000)
            result = tools.execute("read_file", {"path": str(path), "offset": 17, "limit": 500}, max_output_chars=200)
            self.assertEqual(result["content"][:3], "789")
            self.assertLessEqual(len(result["content"]), 200)
            self.assertIn("[TRUNCATED]", result["content"])

    def test_shell_timeout_and_output_is_bounded_with_marker(self):
        with tempfile.TemporaryDirectory() as td:
            marker = str(Path(td) / "late")
            result = tools.execute("shell", {"command": f"sleep 0.5; touch {marker}", "timeout": 0.08})
            self.assertIn("timed out", result["stderr"])
            time.sleep(0.6)
            self.assertFalse(Path(marker).exists(), "timeout should terminate shell descendants")
        command = "printf HEAD; i=0; while [ $i -lt 5000 ]; do printf x; i=$((i+1)); done; printf TAIL"
        result = tools.execute("shell", {"command": command}, max_output_chars=400)
        rendered = result["stdout"] + result["stderr"]
        self.assertLessEqual(len(rendered), 400)
        self.assertIn("[TRUNCATED]", rendered)
        self.assertIn("HEAD", rendered)
        self.assertIn("TAIL", rendered)


class ApprovalTests(unittest.TestCase):
    def test_risky_commands_require_confirmation(self):
        risky = ["rm -rf ./cache", "rm -r ./cache", "/bin/rm -f -r ./cache",
                 "pkg install git", "python -m pip install x", "chmod 777 file", "chown u file",
                 "kill 22", "reboot", "dd if=/dev/zero of=x", "mkfs.ext4 /dev/x",
                 "mount /dev/x /mnt", "adb shell id", "echo x >> ~/.bashrc",
                 "printf x > $PREFIX/etc/profile"]
        for command in risky:
            with self.subTest(command=command):
                self.assertTrue(needs_approval("shell", {"command": command}, "on-risk"))
        self.assertFalse(needs_approval("shell", {"command": "uname -a"}, "on-risk"))
        normal_path = "/data/data/com.termux/files/home/opt/termux-agent/test.txt"
        core_config = "/data/data/com.termux/files/home/.termux/termux.properties"
        self.assertFalse(needs_approval("write_file", {"path": normal_path, "content": "ok"}, "on-risk"))
        self.assertTrue(needs_approval("write_file", {"path": core_config, "content": "ok"}, "on-risk"))


@unittest.skipIf(httpx is None or not hasattr(httpx, "MockTransport"), "httpx MockTransport unavailable")
class ClientTests(unittest.TestCase):
    def test_payload_auth_usage_and_key_is_absent_from_debug_request(self):
        seen = {}

        def respond(request):
            seen["body"] = json.loads(request.content)
            seen["auth"] = request.headers.get("Authorization")
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}],
                                             "usage": {"prompt_tokens": 11, "completion_tokens": 2, "total_tokens": 13}})

        cfg = Config(api_key="secret-test-key", stream=False)
        http = httpx.Client(transport=httpx.MockTransport(respond))
        client = Client(cfg, http_client=http)
        result = client.chat([{"role": "user", "content": "hello"}], [])
        self.assertEqual(result["content"], "ok")
        self.assertEqual(seen["auth"], "Bearer secret-test-key")
        self.assertEqual(seen["body"]["model"], cfg.model)
        self.assertEqual(client.last_usage["total_tokens"], 13)
        self.assertNotIn("secret-test-key", json.dumps(client.last_request))
        http.close()

    def test_http_error_body_and_malformed_response_are_sanitized(self):
        def bad_status(request):
            return httpx.Response(401, text="secret-test-key echoed")

        cfg = Config(api_key="secret-test-key", stream=False)
        http = httpx.Client(transport=httpx.MockTransport(bad_status))
        with self.assertRaises(ClientError) as caught:
            Client(cfg, http_client=http).chat([], [])
        self.assertNotIn("secret-test-key", str(caught.exception))
        self.assertIn("401", str(caught.exception))
        http.close()

        def malformed(request):
            return httpx.Response(200, json={"choices": []})

        http = httpx.Client(transport=httpx.MockTransport(malformed))
        with self.assertRaises(ClientError):
            Client(Config(stream=False), http_client=http).chat([], [])
        http.close()


class ScriptedClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.last_request = None
        self.last_usage = None
        self.sent = []

    def chat(self, messages, schema):
        self.sent.append((json.loads(json.dumps(messages)), schema))
        if not self.replies:
            raise AssertionError("unexpected additional model call")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def call(call_id, name, args):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class AgentTests(unittest.TestCase):
    def agent(self, replies, **kwargs):
        cfg = Config(**kwargs)
        client = ScriptedClient(replies)
        return Agent(cfg, client=client), client

    def test_plain_chat_and_clear(self):
        agent, client = self.agent([{"role": "assistant", "content": "hello"}])
        self.assertEqual(agent.run("hello"), "hello")
        stats = agent.stats()
        self.assertEqual(stats["api_calls_total"], 1)
        self.assertEqual(stats["conversation_count"], 1)
        self.assertEqual(stats["conversation_message_count"], 3)
        self.assertGreater(stats["system_chars"], 0)
        self.assertGreater(stats["tool_schema_chars"], 0)
        self.assertTrue(agent.messages)
        agent.clear()
        self.assertEqual([m["role"] for m in agent.messages], ["system"])
        self.assertEqual(len(client.sent), 1)

    def test_multiple_tool_calls_then_final_answer_valid_history(self):
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "x")
            agent, client = self.agent([
                {"role": "assistant", "content": None, "tool_calls": [call("1", "write_file", {"path": path, "content": "ok"}), call("2", "read_file", {"path": path})]},
                {"role": "assistant", "content": "verified"},
            ], max_steps=3, approval_policy="never")
            self.assertEqual(agent.run("create and check"), "verified")
            self.assertEqual(Path(path).read_text(), "ok")
            history = client.sent[1][0]
            self.assertEqual(history[-3]["role"], "assistant")
            self.assertEqual([m["tool_call_id"] for m in history[-2:]], ["1", "2"])

    def test_tool_errors_reach_model_and_max_steps_is_bounded(self):
        agent, client = self.agent([
            {"role": "assistant", "tool_calls": [call("1", "read_file", {"path": "/missing/not-here"})]},
            {"role": "assistant", "content": "recovered"},
        ])
        self.assertEqual(agent.run("read missing"), "recovered")
        self.assertIn("error", client.sent[1][0][-1]["content"])
        agent, client = self.agent([{"role": "assistant", "tool_calls": [call(str(i), "shell", {"command": "true"})]} for i in range(4)], max_steps=2)
        agent.run("loop")
        self.assertLessEqual(len(client.sent), 2)

    def test_denied_risky_call_is_not_executed(self):
        agent, _ = self.agent([{"role": "assistant", "tool_calls": [call("1", "shell", {"command": "rm -rf nowhere"})]}, {"role": "assistant", "content": "cancelled"}], approval_policy="on-risk")
        agent.approve = lambda name, args: False
        self.assertEqual(agent.run("remove it"), "cancelled")



class CLITests(unittest.TestCase):
    def test_interactive_risk_requires_explicit_yes(self):
        from unittest.mock import patch
        from termux_agent import cli
        captured = []
        class FakeAgent:
            def __init__(self, config, approve, event):
                self.approve = approve
                self.client = type('C', (), {'close': lambda self: None})()
            def run(self, text):
                captured.append(self.approve('shell', {'command': 'rm -rf harmless-test'}))
                return 'done'
        with patch.object(cli, 'Agent', FakeAgent), patch.object(cli.sys, 'argv', ['ta', '--no-session', '--prompt', 'test']), patch.object(cli.sys.stdin, 'isatty', return_value=True), patch('builtins.input', return_value='n'), patch('builtins.print'):
            cli.main()
        self.assertEqual(captured, [False])


if __name__ == "__main__":
    unittest.main()
