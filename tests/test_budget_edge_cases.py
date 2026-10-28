"""Focused regressions for effective output reserve and batch stall handling."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from termux_agent.agent import Agent
from termux_agent.config import Config
from termux_agent.token_budget import TokenCounter


def tool_call(identifier: str, name: str, args: dict) -> dict:
    return {"id": identifier, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


class ScriptedClient:
    history_only = False

    def __init__(self, replies):
        self.replies = list(replies)
        self.sent = []
        self.last_usage = None
        self.last_finish_reason = None
        self.last_attempts = 1

    def set_control_markers(self, markers):
        self.control_markers = tuple(markers)

    def chat(self, messages, tools):
        self.sent.append(copy.deepcopy(messages))
        return copy.deepcopy(self.replies.pop(0))

    def close(self):
        pass


class BudgetEdgeCaseTests(unittest.TestCase):
    def test_effective_output_reserve_and_headroom_cover_explicit_generation_cap(self):
        values = {"context_window_tokens": 1024, "output_reserve_tokens": 512,
                  "max_output_tokens": 8192}
        for mode in ("off", "local", "estimate"):
            with self.subTest(mode=mode):
                config = Config(token_count_mode=mode, **values)
                cache_path = Path(os.environ.get("TMPDIR", "/tmp")) / "termux-agent-budget-edge-cache.json"
                counter = TokenCounter(config, cache_path=cache_path)
                if mode == "local":
                    with patch.object(counter, "_local_metadata",
                                      return_value={"context_window_tokens": 1024}), \
                         patch.object(counter, "_local_count", return_value=(10, 0, True)):
                        measured = counter.measure([{"role": "user", "content": "hello"}], allow_network=True)
                else:
                    measured = counter.measure([{"role": "user", "content": "hello"}], allow_network=False)
                self.assertEqual(measured["output_reserve_tokens"], 8192)

        legacy = Config(token_count_mode="off", context_window_tokens=1024,
                        output_reserve_tokens=512, max_output_tokens=0)
        cache_path = Path(os.environ.get("TMPDIR", "/tmp")) / "termux-agent-budget-edge-cache.json"
        self.assertEqual(TokenCounter(legacy, cache_path=cache_path).measure(
            [], allow_network=False)["output_reserve_tokens"], 512)
        config = Config(context_window_tokens=1024, output_reserve_tokens=512,
                        max_output_tokens=8192)
        with patch.object(config, "validate", return_value=None):
            agent = Agent(config, client=ScriptedClient([]))
        agent.token_counter = type("StubCounter", (), {
            "measure": lambda self, messages, tools, allow_network=True: {
                "prompt_tokens": 900, "context_window_tokens": 1024,
                "output_reserve_tokens": 512,
            }
        })()
        chars, token, exceeded = agent._context_budget([{"role": "user", "content": "hello"}])
        self.assertEqual(token["output_reserve_tokens"], 8192)
        self.assertTrue(exceeded)

    def test_no_progress_threshold_stops_later_calls_in_same_batch(self):
        calls = [tool_call(f"call-{index}", "read_file", {"path": "/tmp/same"})
                 for index in range(5)]
        client = ScriptedClient([{"tool_calls": calls}])
        config = Config(approval_policy="never", no_progress_detection=True,
                        no_progress_threshold=3, auto_continue=True, max_steps=4)
        agent = Agent(config, client=client)
        with patch("termux_agent.agent.execute", return_value={"content": "same"}) as execute:
            result = agent.run("repeat the same read")

        self.assertEqual(result, "连续相同调用、参数和结果达到阈值；结果已保留。")
        self.assertEqual(execute.call_count, 3)
        self.assertEqual(len(client.sent), 1)
        results = [json.loads(message["content"])
                   for message in agent.messages if message.get("role") == "tool"]
        self.assertEqual([item.get("execution_status") for item in results],
                         ["executed"] * 3 + ["not_run"] * 2)
        self.assertTrue(all(item.get("outcome_status") == "not_run" for item in results[3:]))
        self.assertEqual(agent._tool_calls_executed, 3)
        self.assertEqual(agent._tool_calls_not_run, 2)
        self.assertEqual(agent._tool_calls_hard_limit_not_run, 0)
        self.assertTrue(agent._no_progress_stop)
        self.assertEqual(agent.task_state["status"], "stalled")
        self.assertEqual(agent.task_state["stop_reason"], "no_progress")


if __name__ == "__main__":
    unittest.main()
