"""Delegation isolation, limits, approvals and retry integration."""
import copy
import json
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from termux_agent.agent import Agent
from termux_agent.client import Client
from termux_agent.config import Config
from termux_agent.display_privacy import hide_write_payloads
from termux_agent.sessions import snapshot, restore, validate_messages


def call(name, args, identifier="call_a"):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": identifier, "type": "function", "function": {
            "name": name, "arguments": json.dumps(args)}}]}


class ScriptClient:
    last_usage = {"total_tokens": 4}
    last_request = None
    last_attempts = 1

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []
        self.closed = False

    def chat(self, messages, tools):
        self.requests.append(copy.deepcopy((messages, tools)))
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def close(self):
        self.closed = True


class SubagentTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        home = patch.dict(os.environ, {"HOME": temp.name})
        home.start()
        self.addCleanup(home.stop)
        prompt = patch("termux_agent.agent.load_prompt_file", return_value="worker rules")
        prompt.start()
        self.addCleanup(prompt.stop)

    def test_model_delegation_isolates_context_and_inherits_api_and_scope(self):
        root_client = ScriptClient(call("delegate", {"task": "inspect a", "max_steps": 12}),
                                   {"content": "parent conclusion"})
        child_client = ScriptClient({"content": "worker finding", "reasoning_content": "private worker reasoning"})
        config = Config(max_steps=4, approval_policy="never", api_key="test-key")
        agent = Agent(config, client=root_client, base_system="base + directory instructions")
        agent.messages.extend([{"role": "user", "content": "parent private history"},
                               {"role": "assistant", "content": "earlier answer"}])
        agent.load_skill("manual", "parent active skill")
        with patch("termux_agent.agent.Client", return_value=child_client) as factory:
            self.assertEqual(agent.run("use a worker"), "parent conclusion")
        child_config = factory.call_args.args[0]
        self.assertIsNot(child_config, config)
        self.assertEqual((child_config.base_url, child_config.model, child_config.api_key),
                         (config.base_url, config.model, config.api_key))
        self.assertEqual(child_config.max_steps, 4)
        self.assertFalse(child_config.auto_continue)
        messages, tools = child_client.requests[0]
        self.assertEqual(len(messages), 2)
        self.assertIn("directory instructions", messages[0]["content"])
        self.assertNotIn("parent active skill", messages[0]["content"])
        self.assertEqual(messages[1]["content"], "inspect a")
        self.assertNotIn("delegate", [tool["function"]["name"] for tool in tools])
        self.assertIn("delegate", [tool["function"]["name"] for tool in root_client.requests[0][1]])
        self.assertTrue(child_client.closed)
        self.assertNotIn("private worker reasoning", json.dumps(agent.messages))
        result = json.loads(next(m["content"] for m in agent.messages if m["role"] == "tool"))
        self.assertEqual((result["status"], result["summary"]), ("complete", "worker finding"))
        self.assertEqual(agent.stats()["subagents"], {"calls": 1, "api_calls": 1, "tool_calls": 0})
        validate_messages(agent.messages)

    def test_child_tools_keep_approval_and_parent_denial_starts_no_worker(self):
        child = ScriptClient(call("shell", {"command": "rm -rf example"}), {"content": "denied"})
        decisions = []
        agent = Agent(Config(approval_policy="on-risk"), client=ScriptClient(), base_system="base",
                      approve=lambda name, args: decisions.append(name) or False)
        with patch("termux_agent.agent.Client", return_value=child), patch("termux_agent.agent.execute") as execute:
            result = agent.delegate("inspect")
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(decisions, ["shell"])
        execute.assert_not_called()
        self.assertTrue(child.closed)
        agent = Agent(Config(approval_policy="always"), client=ScriptClient(), base_system="base", approve=lambda *_: False)
        with patch("termux_agent.agent.Client") as factory:
            result = agent.delegate("inspect")
        factory.assert_not_called()
        self.assertIn("approval denied", result["error"])
        validate_messages(agent.messages)

    def test_nested_delegation_and_per_task_worker_limits(self):
        children = [ScriptClient(call("delegate", {"task": "nested"}), {"content": "nested unavailable"}),
                    ScriptClient({"content": "second"})]
        root_client = ScriptClient({"content": "", "tool_calls": [
            call("delegate", {"task": str(i)}, f"call_{i}")["tool_calls"][0] for i in range(3)]},
            {"content": "finished"})
        agent = Agent(Config(max_subagents=2, approval_policy="never"), client=root_client, base_system="base")
        with patch("termux_agent.agent.Client", side_effect=children) as factory:
            self.assertEqual(agent.run("delegate"), "finished")
        self.assertEqual(factory.call_count, 2)
        self.assertIn("disabled", children[0].requests[1][0][-1]["content"])
        results = [json.loads(m["content"]) for m in agent.messages if m["role"] == "tool"]
        self.assertIn("limit reached", results[-1]["error"])
        self.assertEqual(agent.stats()["subagents"]["calls"], 2)

    def test_step_limit_and_interrupt_do_not_claim_success_or_leave_pending_tools(self):
        child = ScriptClient(call("read_file", {"path": "a"}))
        agent = Agent(Config(subagent_max_steps=1, approval_policy="never"), client=ScriptClient(), base_system="base")
        with patch("termux_agent.agent.Client", return_value=child), patch("termux_agent.agent.execute") as execute:
            self.assertEqual(agent.delegate("inspect")["status"], "limit")
        execute.assert_not_called()
        self.assertTrue(child.closed)
        interrupted = ScriptClient(KeyboardInterrupt())
        with patch("termux_agent.agent.Client", return_value=interrupted):
            agent.delegate("inspect again")
        self.assertEqual(agent.last_run["status"], "interrupted")
        self.assertTrue(interrupted.closed)
        validate_messages(agent.messages)

    def test_disabled_invalid_and_large_tasks_do_not_open_clients(self):
        agent = Agent(Config(max_subagents=0, approval_policy="never"), client=ScriptClient(), base_system="base")
        self.assertNotIn("delegate", [tool["function"]["name"] for tool in agent.tools])
        with patch("termux_agent.agent.Client") as factory:
            self.assertIn("disabled", agent.delegate("inspect")["error"])
            with self.assertRaises(ValueError):
                agent.delegate("x" * 16001)
        factory.assert_not_called()
        agent = Agent(Config(), client=ScriptClient(), base_system="base")
        for args in ({"task": "a", "max_steps": True}, {"task": "a", "api": "other"}, {"task": ""}):
            with patch("termux_agent.agent.Client") as factory:
                self.assertIn("error", agent._delegate(args))
            factory.assert_not_called()

    def test_subagent_statistics_restore_and_old_snapshots_default_to_zero(self):
        path = self.root / 'config.toml'
        path.write_text('approval_policy="never"\n')
        agent = Agent(Config.load(path), client=ScriptClient(), base_system="base")
        with patch("termux_agent.agent.Client", return_value=ScriptClient({"content": "finding"})):
            agent.delegate("inspect")
        scope = {"directory": str(self.root), "config_path": str(path), "instructions": ""}
        saved = snapshot(agent, scope)
        def build(config, instructions, base_system):
            return Agent(config, client=ScriptClient(), base_system=base_system)
        _, resumed, _ = restore(saved, build)
        self.assertEqual(resumed.stats()["subagents"]["calls"], 1)
        for key in list(saved['counters']):
            if key.startswith('_subagent_'):
                del saved['counters'][key]
        _, resumed, _ = restore(saved, build)
        self.assertEqual(resumed.stats()["subagents"], {"calls": 0, "api_calls": 0, "tool_calls": 0})

    def test_http_retries_inside_worker_do_not_restart_worker_or_repeat_tools(self):
        attempts, sleeps = [], []
        def respond(request):
            attempts.append(request.content)
            if len(attempts) == 1:
                return httpx.Response(500, json={"error": {"type": "api_error"}})
            if len(attempts) == 2:
                return httpx.Response(200, json={"choices": [{"message": call("read_file", {"path": "a"})}]})
            if len(attempts) == 3:
                return httpx.Response(429, headers={"Retry-After": "1"}, json={"error": {"type": "rate_limit_error"}})
            return httpx.Response(200, json={"choices": [{"message": {"content": "found"}}]})
        config = Config(stream=False, approval_policy="never")
        with httpx.Client(transport=httpx.MockTransport(respond)) as http:
            child_client = Client(config, http_client=http, sleeper=sleeps.append, randomizer=lambda: 0.0)
            agent = Agent(config, client=ScriptClient(), base_system="base")
            with patch("termux_agent.agent.Client", return_value=child_client) as factory, \
                    patch("termux_agent.agent.execute", return_value={"content": "found"}) as execute:
                self.assertEqual(agent.delegate("inspect")["status"], "complete")
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(len(attempts), 4)
        self.assertEqual(attempts[0], attempts[1])
        self.assertEqual(attempts[2], attempts[3])
        self.assertEqual(sleeps, [0.5, 1.0])

    def test_task_display_hides_task_payload_without_changing_api_arguments(self):
        message = call("delegate", {"task": "private-task-body"})
        before = copy.deepcopy(message)
        visible = hide_write_payloads(message)
        self.assertNotIn("private-task-body", json.dumps(visible))
        self.assertIn("task_hidden", json.dumps(visible))
        self.assertEqual(message, before)

    def test_cli_sub_command_runs_worker_directly_and_saves_readable_history(self):
        from termux_agent import cli
        root_client, child_client = ScriptClient(), ScriptClient({"content": "worker finding"})
        config = self.root / 'config.toml'
        config.write_text('approval_policy="never"\n')
        out = io.StringIO()
        previous = Path.cwd()
        try:
            with patch.object(cli.sys, 'argv', ['ta', '--config', str(config), '--directory', str(self.root), '--no-agent', '--no-session']), \
                    patch.object(cli.sys, 'stdout', out), \
                    patch('builtins.input', side_effect=['/sub', '/sub inspect', '/history', '/exit']), \
                    patch('termux_agent.agent.Client', side_effect=[root_client, child_client]):
                cli.main()
        finally:
            os.chdir(previous)
        self.assertIn('worker finding', out.getvalue())
        self.assertIn('sub-1 · complete', out.getvalue())
        self.assertIn('History 1–4 / 4', out.getvalue())
        self.assertEqual(root_client.requests, [])
        self.assertTrue(child_client.closed)
