"""Focused RTK statistics, session and delegation regressions.

The shell executor is faked here and publishes ``last_stats`` directly, so these
tests do not depend on the in-flight raw capture/filter implementation.
"""

from __future__ import annotations

import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from termux_agent.agent import Agent
from termux_agent.config import Config
from termux_agent.sessions import SessionStore, new_id, restore, snapshot
from termux_agent.token_budget import TokenCounter


class FakeClient:
    last_attempts = 1
    last_usage = {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}

    def __init__(self):
        self.closed = False

    def chat(self, messages, tools):
        raise AssertionError("focused statistics tests must not call a provider")

    def close(self):
        self.closed = True


def tool_call(identifier, name="shell", args=None):
    return {"id": identifier, "type": "function", "function": {
        "name": name, "arguments": json.dumps(args or {"command": "pytest"})}}


def last_stats(*, status="compressed", filter_attempted=True, applied=True,
               raw_capture_complete=True, raw_available=True, raw_artifact=None,
               before_bytes=1000, after_bytes=300, before_chars=900, after_chars=250):
    return {
        "status": status,
        "filter_attempted": filter_attempted,
        "applied": applied,
        "raw_capture_complete": raw_capture_complete,
        "raw_available": raw_available,
        "raw_artifact": raw_artifact,
        "before_bytes": before_bytes,
        "after_bytes": after_bytes,
        "before_chars": before_chars,
        "after_chars": after_chars,
    }


class RtkAgentStatsTests(unittest.TestCase):
    def make_agent(self, *, rtk_enabled=True):
        config = Config(rtk_enabled=rtk_enabled, approval_policy="never",
                        token_count_mode="off")
        return Agent(config, client=FakeClient(), base_system="base")

    def test_model_shell_consumes_last_stats_and_counts_only_applied_sizes(self):
        agent = self.make_agent()
        values = iter([
            last_stats(raw_artifact={"id": "raw-a", "bytes": 222}),
            last_stats(status="no_benefit", filter_attempted=False, applied=False,
                       raw_available=False, raw_artifact=None,
                       before_bytes=800, after_bytes=1,
                       before_chars=700, after_chars=1),
            None,
        ])
        calls = []

        def fake_execute(name, args, max_output_chars, **kwargs):
            calls.append((name, args, max_output_chars, kwargs))
            agent.output_compressor.last_stats = next(values)
            return {"exit_code": 0, "stdout": "ok", "stderr": ""}

        with patch("termux_agent.agent.execute", side_effect=fake_execute):
            interrupted = agent._fill_tool_results([
                tool_call("one"), tool_call("two"), tool_call("three", args={"command": "uname"}),
            ])

        self.assertFalse(interrupted)
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(call[3].get("output_compressor") is agent.output_compressor
                            for call in calls))
        self.assertIsNone(agent.output_compressor.last_stats)
        compression = agent.stats()["output_compression"]
        self.assertEqual(compression["shell_results"], 2)
        self.assertEqual(compression["filter_attempts"], 1)
        self.assertEqual(compression["compressed_results"], 1)
        self.assertEqual(compression["raw_artifacts"], 1)
        self.assertEqual(compression["artifact_bytes"], 222)
        self.assertEqual((compression["before_bytes"], compression["after_bytes"]),
                         (1000, 300))
        self.assertEqual((compression["before_chars"], compression["after_chars"]),
                         (900, 250))
        self.assertEqual(compression["bytes_saved"], 700)
        self.assertEqual(compression["chars_saved"], 650)

    def test_failure_artifact_and_capture_counters_do_not_create_savings(self):
        agent = self.make_agent()
        value = last_stats(status="filter_failed", applied=False,
                           raw_capture_complete=False, raw_available=True,
                           raw_artifact={"id": "raw-failure", "bytes": 444},
                           before_bytes=99999, after_bytes=1,
                           before_chars=88888, after_chars=1)

        def fake_execute(name, args, max_output_chars, **kwargs):
            agent.output_compressor.last_stats = value
            return {"exit_code": 7, "stdout": "failure", "stderr": "detail"}

        with patch("termux_agent.agent.execute", side_effect=fake_execute):
            agent._fill_tool_results([tool_call("failure")])

        compression = agent.stats()["output_compression"]
        self.assertEqual(compression["filter_failures"], 1)
        self.assertEqual(compression["incomplete_capture"], 1)
        self.assertEqual(compression["raw_artifacts"], 1)
        self.assertEqual(compression["artifact_bytes"], 444)
        self.assertEqual(compression["filter_attempts"], 1)
        self.assertEqual((compression["before_bytes"], compression["after_bytes"]), (0, 0))
        self.assertEqual((compression["before_chars"], compression["after_chars"]), (0, 0))
        self.assertEqual((compression["bytes_saved"], compression["chars_saved"]), (0, 0))

    def test_disabled_shell_keeps_legacy_execute_kwargs_and_zero_stats(self):
        agent = self.make_agent(rtk_enabled=False)
        calls = []

        def fake_execute(name, args, max_output_chars, **kwargs):
            calls.append(kwargs)
            agent.output_compressor.last_stats = last_stats()
            return {"exit_code": 0, "stdout": "raw", "stderr": ""}

        with patch("termux_agent.agent.execute", side_effect=fake_execute):
            agent._fill_tool_results([tool_call("raw")])

        self.assertEqual(calls, [{}])
        self.assertIsNone(agent.output_compressor.last_stats)
        compression = agent.stats()["output_compression"]
        self.assertFalse(compression["enabled"])
        self.assertEqual(compression["shell_results"], 0)
        self.assertEqual(compression["bytes_saved"], 0)

    def test_manual_shell_cli_is_raw_and_never_consumes_compressor_stats(self):
        from termux_agent import cli

        calls = []

        def fake_execute(name, args, max_output_chars, **kwargs):
            calls.append((name, args, max_output_chars, kwargs))
            return {"exit_code": 0, "stdout": "manual-raw\n", "stderr": ""}

        original_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                config = Path(directory) / "config.toml"
                config.write_text('approval_policy="never"\ntoken_count_mode="off"\n')
                output = io.StringIO()
                with patch.object(cli.sys, "argv", [
                        "ta", "--config", str(config), "--directory", directory,
                        "--no-agent", "--no-session"]), \
                        patch.object(cli.sys, "stdout", output), \
                        patch("builtins.input", side_effect=["!printf manual-raw", "/exit"]), \
                        patch("termux_agent.tools.execute", side_effect=fake_execute), \
                        patch.object(Agent, "_record_output_compression") as record_stats:
                    cli.main()
            finally:
                os.chdir(original_cwd)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][3], {})
        self.assertIn("manual-raw", output.getvalue())
        record_stats.assert_not_called()

    def test_stable_fingerprint_uses_existing_module_helper(self):
        agent = self.make_agent()
        result = {"exit_code": 0, "stdout": "same", "stderr": ""}

        def fake_execute(name, args, max_output_chars, **kwargs):
            agent.output_compressor.last_stats = None
            return dict(result)

        with patch("termux_agent.agent.execute", side_effect=fake_execute), \
                patch("termux_agent.agent.stable_result_payload",
                      return_value={"stable": True}) as stable_payload:
            agent._fill_tool_results([tool_call("stable")])

        stable_payload.assert_called_once()
        self.assertEqual(stable_payload.call_args.args[0]["stdout"], "same")


class RtkSessionStatsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config_path = self.root / "config.toml"
        self.config_path.write_text(
            'base_url="https://example.invalid/v1"\nmodel="test-model"\n'
            'approval_policy="never"\nrtk_enabled=true\ntoken_count_mode="off"\n')
        self.scope = {
            "directory": str(self.root), "config_path": str(self.config_path),
            "instructions": "saved rule", "session_file": None,
            "disabled": False, "instruction_path": None,
        }

    @staticmethod
    def build_agent(config, instructions, base_system=None, client=None):
        return Agent(config, client=client or FakeClient(), instructions=instructions,
                     base_system=base_system)

    @staticmethod
    def token_measure(messages, tools, allow_network=True):
        return {"prompt_tokens": 1, "source": "test", "allow_network": allow_network}

    def test_counters_persist_and_legacy_snapshots_restore_to_zero(self):
        agent = Agent(Config.load(self.config_path), client=FakeClient(),
                      instructions="saved rule", base_system="base")
        agent._record_output_compression(last_stats(raw_artifact={"bytes": 222}))
        agent._record_output_compression(last_stats(
            status="filter_failed", applied=False, raw_capture_complete=False,
            raw_available=True, raw_artifact={"bytes": 444},
            before_bytes=10, after_bytes=1, before_chars=9, after_chars=1))
        saved = snapshot(agent, self.scope)

        with patch.object(TokenCounter, "measure", side_effect=self.token_measure):
            _, resumed, _ = restore(saved, self.build_agent)
            resumed_stats = resumed.stats()["output_compression"]
        self.assertEqual(resumed_stats["filter_attempts"], 2)
        self.assertEqual(resumed_stats["compressed_results"], 1)
        self.assertEqual(resumed_stats["filter_failures"], 1)
        self.assertEqual(resumed_stats["incomplete_capture"], 1)
        self.assertEqual(resumed_stats["raw_artifacts"], 2)
        self.assertEqual(resumed_stats["artifact_bytes"], 666)
        self.assertEqual(resumed_stats["bytes_saved"], 700)
        self.assertEqual(resumed_stats["chars_saved"], 650)

        legacy = copy.deepcopy(saved)
        legacy["counters"] = {
            key: value for key, value in legacy["counters"].items()
            if not key.startswith("_rtk_")
        }
        with patch.object(TokenCounter, "measure", side_effect=self.token_measure):
            _, old, _ = restore(legacy, self.build_agent)
            old_stats = old.stats()["output_compression"]
        self.assertEqual(old_stats["shell_results"], 0)
        self.assertEqual(old_stats["filter_attempts"], 0)
        self.assertEqual(old_stats["raw_artifacts"], 0)
        self.assertEqual((old_stats["bytes_saved"], old_stats["chars_saved"]), (0, 0))

    def test_history_only_view_is_offline_and_does_not_rewrite_archived_row(self):
        agent = Agent(Config.load(self.config_path), client=FakeClient(),
                      instructions="saved rule", base_system="base")
        saved = snapshot(agent, self.scope)
        store = SessionStore(self.root / "store")
        self.addCleanup(store.close)
        identifier = new_id()
        store.save(identifier, 0, saved)
        row_before = store.db.execute(
            "SELECT payload,revision,updated FROM sessions WHERE id=?", (identifier,)).fetchone()

        with patch("termux_agent.sessions.Config.load", side_effect=ValueError("offline")):
            _, history, _ = restore(saved, self.build_agent, history_fallback=True)
        self.assertTrue(history.history_only)
        with patch.object(history.token_counter, "measure", side_effect=self.token_measure) as measure:
            history.stats()
        self.assertFalse(measure.call_args.kwargs["allow_network"])
        with self.assertRaises(ValueError):
            history.run("must not reach a provider")

        row_after = store.db.execute(
            "SELECT payload,revision,updated FROM sessions WHERE id=?", (identifier,)).fetchone()
        self.assertEqual(row_after, row_before)


class RtkSubagentCounterTests(unittest.TestCase):
    @staticmethod
    def fill_child_counters(agent):
        agent._rtk_shell_results = 2
        agent._rtk_filter_attempts = 1
        agent._rtk_compressed_results = 1
        agent._rtk_filter_failures = 1
        agent._rtk_incomplete_capture = 1
        agent._rtk_raw_artifacts = 1
        agent._rtk_artifact_bytes = 444
        agent._rtk_before_bytes = 1000
        agent._rtk_after_bytes = 300
        agent._rtk_before_chars = 900
        agent._rtk_after_chars = 250

    def make_parent(self):
        config = Config(rtk_enabled=True, approval_policy="never",
                        token_count_mode="off", max_subagents=1)
        return Agent(config, client=FakeClient(), base_system="parent")

    def test_normal_child_stats_once_and_merge_primitive_counters_once(self):
        parent = self.make_parent()
        child_client = FakeClient()
        stats_calls, measure_calls = [], []
        original_stats, original_merge = Agent.stats, Agent._merge_output_compression

        def child_run(child, task, auto_continue=False):
            RtkSubagentCounterTests.fill_child_counters(child)
            return "child finding"

        def stats(self):
            stats_calls.append(self)
            return original_stats(self)

        def measure(self, messages, tools, allow_network=True):
            measure_calls.append(self)
            return {"prompt_tokens": 1, "source": "test"}

        def merge(self, values):
            original_merge(self, values)

        with patch("termux_agent.agent.Client", return_value=child_client), \
                patch("termux_agent.agent.load_prompt_file", return_value="worker rules"), \
                patch.object(Agent, "run", autospec=True, side_effect=child_run), \
                patch.object(Agent, "stats", autospec=True, side_effect=stats), \
                patch.object(Agent, "_merge_output_compression", autospec=True, side_effect=merge) as merged, \
                patch.object(TokenCounter, "measure", autospec=True, side_effect=measure):
            result = parent._delegate({"task": "inspect"})

        self.assertEqual(result["summary"], "child finding")
        self.assertEqual(len(stats_calls), 1)
        self.assertEqual(len(measure_calls), 1)
        self.assertEqual(merged.call_count, 1)
        primitive = merged.call_args.args[1]
        self.assertEqual(primitive["_rtk_shell_results"], 2)
        self.assertEqual(primitive["_rtk_before_bytes"], 1000)
        self.assertEqual(parent._rtk_artifact_bytes, 444)
        self.assertTrue(child_client.closed)

    def test_interrupted_child_merges_primitive_counters_once_without_stats_measurement(self):
        parent = self.make_parent()
        child_client = FakeClient()
        stats_calls, measure_calls = [], []
        original_stats, original_merge = Agent.stats, Agent._merge_output_compression

        def child_run(child, task, auto_continue=False):
            RtkSubagentCounterTests.fill_child_counters(child)
            raise KeyboardInterrupt

        def stats(self):
            stats_calls.append(self)
            return original_stats(self)

        def measure(self, messages, tools, allow_network=True):
            measure_calls.append(self)
            return {"prompt_tokens": 1, "source": "test"}

        def merge(self, values):
            original_merge(self, values)

        with patch("termux_agent.agent.Client", return_value=child_client), \
                patch("termux_agent.agent.load_prompt_file", return_value="worker rules"), \
                patch.object(Agent, "run", autospec=True, side_effect=child_run), \
                patch.object(Agent, "stats", autospec=True, side_effect=stats), \
                patch.object(Agent, "_merge_output_compression", autospec=True, side_effect=merge) as merged, \
                patch.object(TokenCounter, "measure", autospec=True, side_effect=measure):
            with self.assertRaises(KeyboardInterrupt):
                parent._delegate({"task": "inspect"})

        self.assertEqual(stats_calls, [])
        self.assertEqual(measure_calls, [])
        self.assertEqual(merged.call_count, 1)
        self.assertEqual(parent._rtk_shell_results, 2)
        self.assertEqual(parent._rtk_filter_failures, 1)
        self.assertEqual(parent._rtk_before_bytes, 1000)
        self.assertTrue(child_client.closed)


class RtkReadmeContractTests(unittest.TestCase):
    def test_readme_matches_owned_contract_and_provider_cleanup(self):
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
        self.assertNotIn("agentrouter", readme.casefold())
        for required in (
                "disabled by default", "rtk_enabled", "[apis.*]", "Manual",
                "stdout_text", "stderr_text", "8 MiB", "32-file", "1 MiB",
                "best-effort", "applied", "legacy clipping", "not actual tokenizer"):
            self.assertIn(required.casefold(), readme.casefold())


if __name__ == "__main__":
    unittest.main()
