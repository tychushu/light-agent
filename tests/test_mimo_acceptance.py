"""Independent MiMo acceptance coverage for user-visible reliability invariants.

This file is exclusively owned by the MiMo acceptance-test worker. The other MiMo
worker owns production fixes and existing unit tests; do not change production
code or weaken these assertions to accommodate an incomplete implementation.

All network-capable paths are deterministic fakes or httpx.MockTransport. CLI
workflows run in child processes with the required RAM-disk HOME and TMPDIR.
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import httpx

from termux_agent.agent import Agent
from termux_agent.client import Client, ContextLengthError, IncompleteOutputError, NetworkError
from termux_agent.config import Config
from termux_agent.sessions import SessionStore, restore, snapshot
from termux_agent.token_budget import TokenCounter, estimate_text_tokens


REPO_ROOT = Path(__file__).resolve().parents[1]


def call(identifier: str, name: str, args: dict) -> dict:
    return {"id": identifier, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


def tool_result(identifier: str, value: dict) -> dict:
    return {"role": "tool", "tool_call_id": identifier,
            "content": json.dumps(value, ensure_ascii=False, sort_keys=True)}


def text_item(text: str) -> dict:
    return {"type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": text}]}


class ScriptedClient:
    """Small deterministic client contract used only to drive public Agent APIs."""

    history_only = False

    def __init__(self, replies):
        self.replies = list(replies)
        self.sent = []
        self.last_usage = None
        self.last_finish_reason = None
        self.last_attempts = 1
        self.last_request = None
        self.streamed = self.streamed_text = False

    def set_control_markers(self, markers):
        self.control_markers = tuple(markers)

    def chat(self, messages, tools):
        request = copy.deepcopy(messages)
        self.sent.append(request)
        self.last_request = {"messages": request, "tools": copy.deepcopy(tools)}
        if not self.replies:
            raise AssertionError("scripted client received an unexpected request")
        spec = self.replies.pop(0)
        self.last_usage = copy.deepcopy(spec.get("_usage"))
        self.last_finish_reason = spec.get("_finish_reason")
        self.last_attempts = spec.get("_attempts", 1)
        if "_raise" in spec:
            raise spec["_raise"]
        return copy.deepcopy(spec.get("_reply", spec))

    def close(self):
        pass


class NetworkBombClient:
    def __init__(self, *args, **kwargs):
        raise AssertionError("history-only acceptance created a network client")


@contextmanager
def open_store(path):
    store = SessionStore(path)
    try:
        yield store
    finally:
        store.close()


CLI_DRIVER = r'''
import importlib
import io
import json
import os
from pathlib import Path
import traceback
from unittest.mock import patch

scenario = json.loads(Path(os.environ["TA_SCENARIO"]).read_text(encoding="utf-8"))
cli = importlib.import_module("termux_agent.cli")
sessions = importlib.import_module("termux_agent.sessions")
tools = importlib.import_module("termux_agent.tools")
client_module = importlib.import_module("termux_agent.client")

requests_path = Path(scenario["requests"])
requests_path.touch()
responses = list(scenario.get("responses", []))

class FakeClient:
    def __init__(self, config):
        self.config = config
        self.last_usage = None
        self.last_finish_reason = None
        self.last_attempts = 1
        self.last_request = None
        self.streamed = self.streamed_text = False
        self.on_stream = None
        self.on_retry = None

    def set_control_markers(self, markers):
        pass

    def chat(self, messages, tools):
        body = {"messages": messages, "tools": tools}
        self.last_request = body
        with requests_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(body, ensure_ascii=False) + "\n")
        if not responses:
            raise AssertionError("CLI fake client exhausted its scripted responses")
        spec = responses.pop(0)
        if spec.get("external_update"):
            update = spec["external_update"]
            external = sessions.SessionStore(Path(scenario["store"]))
            try:
                revision, payload = external.load(update["id"])
                payload["messages"].append({"role": "assistant", "content": update["content"]})
                external.save(update["id"], revision, payload)
                external.rename(update["id"], update["title"])
                row = external.db.execute(
                    "SELECT title,updated,revision,payload FROM sessions WHERE id=?",
                    (update["id"],),
                ).fetchone()
                Path(update["record"]).write_text(json.dumps(list(row)), encoding="utf-8")
            finally:
                external.close()
        self.last_usage = spec.get("usage")
        self.last_finish_reason = spec.get("finish_reason")
        self.last_attempts = spec.get("attempts", 1)
        kind = spec.get("kind", "message")
        if kind == "incomplete":
            raise client_module.IncompleteOutputError(spec["text"], spec["message"])
        if kind == "context":
            raise client_module.ContextLengthError(spec["text"], pre_accept=spec.get("pre_accept", True))
        return spec.get("message", {key: value for key, value in spec.items()
                                    if key not in ("kind", "usage", "finish_reason", "attempts")})

    def close(self):
        pass


class NetworkBombClient:
    def __init__(self, *args, **kwargs):
        raise AssertionError("history-only workflow created an HTTP client")

def factory(*args, **kwargs):
    if scenario.get("bomb_agent_client"):
        raise AssertionError("history-only workflow constructed Agent.Client")
    return FakeClient(*args, **kwargs)

out = io.StringIO()
err = io.StringIO()
patches = [
    patch.object(cli.sys, "argv", ["ta", *scenario["argv"]]),
    patch("builtins.input", side_effect=scenario["prompts"]),
    patch.object(cli.sys, "stdout", out),
    patch.object(cli.sys, "stderr", err),
    patch("termux_agent.agent.Client", factory),
]
if scenario.get("store"):
    store_root = Path(scenario["store"])
    patches.append(patch.object(cli, "SessionStore", lambda: sessions.SessionStore(store_root)))
if scenario.get("bomb_network"):
    patches.append(patch("termux_agent.token_budget.httpx.Client", NetworkBombClient))
for item in patches:
    item.start()
exit_code = 0
try:
    cli.main()
except BaseException as exc:
    exit_code = 1
    err.write(traceback.format_exc())
finally:
    for item in reversed(patches):
        item.stop()
Path(scenario["output"]).write_text(out.getvalue(), encoding="utf-8")
Path(scenario["stderr"]).write_text(err.getvalue(), encoding="utf-8")
raise SystemExit(exit_code)
'''


CHECKPOINT_DEATH_DRIVER = r'''
import importlib
import json
import os
from pathlib import Path
import traceback
from unittest.mock import patch

cli = importlib.import_module("termux_agent.cli")
agent_module = importlib.import_module("termux_agent.agent")
sessions = importlib.import_module("termux_agent.sessions")
tools = importlib.import_module("termux_agent.tools")
root = Path(os.environ["TA_DEATH_ROOT"])
project = root / "project"
project.mkdir(exist_ok=True)
target = project / "harmless.txt"
store_root = root / "store"
config_path = root / "checkpoint-death.toml"
session_id = os.environ["TA_DEATH_SESSION"]

class FakeClient:
    def __init__(self, config):
        self.config = config
        self.last_usage = None
        self.last_finish_reason = None
        self.last_attempts = 1
        self.last_request = None
        self.streamed = self.streamed_text = False
        self.on_stream = None
        self.on_retry = None
        self.calls = 0

    def set_control_markers(self, markers):
        pass

    def chat(self, messages, tools):
        self.calls += 1
        if self.calls != 1:
            raise AssertionError("checkpoint-death fake model was called more than once")
        return {"tool_calls": [{
            "id": "death-write", "type": "function",
            "function": {"name": "write_file", "arguments": json.dumps({
                "path": str(target), "content": "harmless side effect",
            }, ensure_ascii=False)},
        }]}

    def close(self):
        pass

def factory(*args, **kwargs):
    return FakeClient(*args, **kwargs)

real_save = sessions.SessionStore.save

def save_then_die(store, identifier, revision, payload, **kwargs):
    next_revision = real_save(store, identifier, revision, payload, **kwargs)
    pending = [item for message in payload.get("messages", [])
               for item in message.get("tool_calls", [])
               if item.get("id") == "death-write"]
    if payload.get("checkpoint") and pending:
        result = tools.execute("write_file", {
            "path": str(target), "content": "harmless side effect",
        })
        if result.get("error") or result.get("verified") is not True:
            raise RuntimeError("harmless write did not verify: " + repr(result))
        os._exit(17)
    return next_revision

patches = [
    patch.object(cli.sys, "argv", ["ta", "--session", session_id]),
    patch("builtins.input", side_effect=["perform harmless write"]),
    patch.object(cli, "SessionStore", lambda: sessions.SessionStore(store_root)),
    patch("termux_agent.agent.Client", factory),
    patch.object(sessions.SessionStore, "save", save_then_die),
]
for item in patches:
    item.start()
exit_code = 0
try:
    cli.main()
except BaseException as exc:
    exit_code = 98
    traceback.print_exc()
finally:
    for item in reversed(patches):
        item.stop()
raise SystemExit(exit_code)
'''


class AcceptanceTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            prefix="ta-mimo-acceptance-", dir=os.environ.get("TMPDIR") or None)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.child_tmp = self.root / "tmp"
        self.home.mkdir()
        self.child_tmp.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def config(self, **changes):
        values = dict(base_url="http://127.0.0.1:9/v1", model="acceptance-model",
                      approval_policy="never", stream=False, token_count_mode="off",
                      context_window_tokens=0, max_task_calls=32)
        values.update(changes)
        config = Config(**values)
        config.validate()
        return config

    def run_cli(self, argv, prompts, responses=(), *, store=None, bomb_network=False,
                bomb_agent_client=False):
        scenario = self.root / ("scenario-" + str(len(list(self.root.glob("scenario-*")))) + ".json")
        output = scenario.with_suffix(".out")
        stderr = scenario.with_suffix(".err")
        requests = scenario.with_suffix(".requests.jsonl")
        scenario.write_text(json.dumps({
            "argv": list(argv), "prompts": list(prompts), "responses": list(responses),
            "store": str(store) if store else "", "bomb_network": bomb_network,
            "bomb_agent_client": bomb_agent_client, "output": str(output),
            "stderr": str(stderr), "requests": str(requests),
        }, ensure_ascii=False), encoding="utf-8")
        driver = self.root / ("cli-driver-" + scenario.stem + ".py")
        driver.write_text(CLI_DRIVER, encoding="utf-8")
        env = os.environ.copy()
        env.update({"HOME": str(self.home), "TMPDIR": str(self.child_tmp),
                    "PYTHONPATH": str(REPO_ROOT), "TA_SCENARIO": str(scenario)})
        completed = subprocess.run([sys.executable, str(driver)], cwd=REPO_ROOT, env=env,
                                   text=True, capture_output=True, timeout=30, check=False)
        child_stderr = stderr.read_text(encoding="utf-8") if stderr.exists() else ""
        self.assertEqual(completed.returncode, 0, completed.stderr + child_stderr)
        return output.read_text(encoding="utf-8"), requests


class BoundedContinuationTests(AcceptanceTestCase):
    def test_one_user_task_runs_17_unique_tools_across_segments_then_respects_hard_cap(self):
        source = self.root / "source.txt"
        source.write_text("".join(chr(65 + i) + "\n" for i in range(20)), encoding="utf-8")
        replies = [{"tool_calls": [call(f"u{i:02d}", "read_file",
                                         {"path": str(source), "offset": i, "limit": 1})]}
                   for i in range(17)] + [{"content": "all distinct calls complete [TASK_DONE]"}]
        client = ScriptedClient(replies)
        agent = Agent(self.config(max_steps=16, max_task_calls=18, auto_continue=True,
                                  auto_extend_segments=True, no_progress_detection=True,
                                  no_progress_threshold=2), client=client)
        with patch("termux_agent.agent.load_prompt_file", return_value="bounded automatic rules"):
            self.assertEqual(agent.run("inspect all offsets"), "all distinct calls complete")
        stats = agent.stats()
        self.assertEqual(stats["tool_calls_executed"], 17)
        self.assertEqual(stats["model_logical_calls_total"], 18)
        self.assertEqual(agent.task_state["logical_calls"], 18)
        self.assertGreaterEqual(agent.task_state["segments"], 2)
        self.assertNotEqual(agent.last_run["stop_reason"], "stalled")
        non_control_users = [m for m in agent.messages if m["role"] == "user"
                             and not str(m.get("content", "")).startswith("[TASK_")]
        self.assertEqual(len(non_control_users), 1)

        before = len(client.sent)
        with patch("termux_agent.agent.load_prompt_file", return_value="bounded resume rules"):
            agent.continue_task(steps=1)
        self.assertEqual(len(client.sent), before)
        self.assertEqual(agent.stats()["model_logical_calls_total"], 18)
        self.assertEqual(agent.last_run["stop_reason"], "task_call_budget")

    def test_legacy_known_not_run_write_is_reissued_and_executed_once_after_continue(self):
        target = self.root / "legacy-write.txt"
        config_path = self.root / "legacy.toml"
        config_path.write_text('base_url="http://127.0.0.1:9/v1"\nmodel="acceptance-model"\n'
                               'max_task_calls=32\nmax_steps=16\n', encoding="utf-8")
        cfg = Config.load(config_path)
        agent = Agent(cfg, client=ScriptedClient([]), instructions="saved instructions")
        agent.messages.append({"role": "user", "content": "finish the legacy write"})
        for index in range(15):
            identifier = f"legacy{index:02d}"
            current = call(identifier, "read_file", {"path": str(config_path), "offset": index, "limit": 1})
            agent.messages.extend([{"role": "assistant", "content": "", "tool_calls": [current]},
                                   tool_result(identifier, {"content": "x", "offset": index})])
        last = call("legacywrite", "write_file", {"path": str(target), "content": "written once"})
        agent.messages.extend([
            {"role": "assistant", "content": "", "tool_calls": [last]},
            tool_result("legacywrite", {"error": "maximum API request count reached",
                                        "execution_status": "not_run"}),
        ])
        agent.last_run = {"status": "limit", "stop_reason": "step_limit",
                          "message": "maximum API request count reached"}
        scope = {"directory": str(self.root), "config_path": str(config_path),
                 "instructions": "saved instructions", "session_file": None,
                 "disabled": False, "instruction_path": None}
        identifier = "abcdef123456"
        with open_store(self.root / "store") as store:
            store.save(identifier, 0, snapshot(agent, scope))
            revision, payload = store.load(identifier)

        reissued = call("newwriteid", "write_file", {"path": str(target), "content": "written once"})
        holder = {}
        def build(config, text, base_system=None, client=None):
            holder["client"] = ScriptedClient([{"tool_calls": [reissued]}, {"content": "write complete"}])
            return Agent(config, client=client or holder["client"], instructions=text,
                         base_system=base_system)
        _, restored, _ = restore(payload, build)
        with patch("termux_agent.agent.load_prompt_file", return_value="bounded resume rules"):
            self.assertEqual(restored.continue_task(steps=2), "write complete")
        self.assertEqual(target.read_text(encoding="utf-8"), "written once")
        self.assertEqual(restored.stats()["tool_calls_executed"], 1)
        self.assertFalse(target.read_text(encoding="utf-8").endswith("written oncwritten once"))

    def test_unknown_legacy_write_is_never_blindly_repeated(self):
        target = self.root / "unknown-write.txt"
        config_path = self.root / "unknown.toml"
        config_path.write_text('base_url="http://127.0.0.1:9/v1"\nmodel="acceptance-model"\n', encoding="utf-8")
        agent = Agent(Config.load(config_path), client=ScriptedClient([]))
        agent.messages.append({"role": "user", "content": "recover an interrupted write"})
        pending = call("unknownwrite", "write_file", {"path": str(target), "content": "maybe"})
        agent.messages.extend([
            {"role": "assistant", "content": "", "tool_calls": [pending]},
            tool_result("unknownwrite", {"error": "Interrupted checkpoint: tool outcome is unknown",
                                         "execution_status": "unknown"}),
        ])
        scope = {"directory": str(self.root), "config_path": str(config_path),
                 "instructions": "", "session_file": None, "disabled": False, "instruction_path": None}
        payload = snapshot(agent, scope)
        reissued = call("retrywrite", "write_file", {"path": str(target), "content": "maybe"})
        holder = {}
        def build(config, text, base_system=None, client=None):
            holder["client"] = ScriptedClient([{"tool_calls": [reissued]}, {"content": "stop"}])
            return Agent(config, client=client or holder["client"], instructions=text,
                         base_system=base_system)
        _, restored, _ = restore(payload, build)
        with patch("termux_agent.agent.load_prompt_file", return_value="bounded resume rules"):
            restored.continue_task(steps=2)
        self.assertFalse(target.exists())
        results = [json.loads(m["content"]) for m in restored.messages if m["role"] == "tool"]
        self.assertTrue(any(item.get("execution_status") == "not_run" for item in results[-1:]))
        self.assertGreaterEqual(restored.stats()["tool_calls_not_run"], 1)


class ProcessCheckpointTests(AcceptanceTestCase):
    def test_auto_compact_checkpoint_survives_death_after_harmless_write(self):
        project = self.root / "project"
        project.mkdir()
        config_path = self.root / "checkpoint-death.toml"
        config_path.write_text(
            'base_url="http://127.0.0.1:9/v1"\nmodel="acceptance-model"\n'
            'approval_policy="never"\nstream=false\ntoken_count_mode="off"\n'
            'max_steps=2\nmax_task_calls=4\nmax_context_chars=100000\n'
            'auto_compact_task=true\nauto_compact_threshold=0.95\n',
            encoding="utf-8",
        )
        store_root = self.root / "store"
        cfg = Config.load(config_path)
        agent = Agent(cfg, client=ScriptedClient([{"content": "existing run saved"}]))
        self.assertEqual(agent.run("existing run"), "existing run saved")
        scope = {"directory": str(project), "config_path": str(config_path),
                 "instructions": "", "session_file": None, "disabled": True,
                 "instruction_path": None}
        session_id = "abcdef123456"
        with open_store(store_root) as store:
            store.save(session_id, 0, snapshot(agent, scope))
            initial_revision = store.load(session_id)[0]

        driver = self.root / "checkpoint-death-driver.py"
        driver.write_text(CHECKPOINT_DEATH_DRIVER, encoding="utf-8")
        env = os.environ.copy()
        env.update({"HOME": str(self.home), "TMPDIR": str(self.child_tmp),
                    "PYTHONPATH": str(REPO_ROOT), "TA_DEATH_ROOT": str(self.root),
                    "TA_DEATH_SESSION": session_id})
        completed = subprocess.run([sys.executable, str(driver)], cwd=REPO_ROOT, env=env,
                                   text=True, capture_output=True, timeout=30, check=False)
        self.assertEqual(completed.returncode, 17, completed.stdout + completed.stderr)

        target = self.root / "project" / "harmless.txt"
        self.assertEqual(target.read_text(encoding="utf-8"), "harmless side effect")
        with open_store(self.root / "store") as store:
            revision, payload = store.load(session_id)

        self.assertGreater(revision, initial_revision)
        self.assertTrue(payload["config"]["auto_compact_task"])
        self.assertEqual(payload["config"]["auto_compact_threshold"], 0.95)
        self.assertEqual(payload["task_state"]["compactions"], 0)
        self.assertTrue(any(message.get("role") == "user"
                            and message.get("content") == "perform harmless write"
                            for message in payload["messages"]))
        calls = [message for message in payload["messages"]
                 if message.get("role") == "assistant" and message.get("tool_calls")]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["tool_calls"][0]["id"], "death-write")
        pending = [message for message in payload["messages"]
                   if message.get("role") == "tool"
                   and message.get("tool_call_id") == "death-write"]
        self.assertEqual(len(pending), 1)
        self.assertTrue(payload["checkpoint"])
        outcome = json.loads(pending[0]["content"])
        self.assertIn("outcome is unknown", outcome["error"])
        self.assertIn("do not assume success or replay automatically", outcome["error"])

        replay = Mock(side_effect=AssertionError("reopen automatically replayed the pending write"))
        def build(config, text, base_system=None, client=None):
            return Agent(config, client=client or ScriptedClient([]), instructions=text,
                         base_system=base_system)
        with patch("termux_agent.agent.execute", replay):
            _, restored, _ = restore(payload, build)
        replay.assert_not_called()
        self.assertEqual(target.read_text(encoding="utf-8"), "harmless side effect")
        self.assertEqual(restored.messages[-1]["tool_call_id"], "death-write")


class StopAndProgressTests(AcceptanceTestCase):
    def test_approval_denial_stops_batch_and_marks_remaining_calls_not_run(self):
        first = self.root / "denied-a.txt"
        second = self.root / "denied-b.txt"
        calls = [call("d1", "write_file", {"path": str(first), "content": "a"}),
                 call("d2", "write_file", {"path": str(second), "content": "b"})]
        approvals = []
        client = ScriptedClient([{"tool_calls": calls}])
        agent = Agent(self.config(approval_policy="always", max_steps=1, max_task_calls=5,
                                  auto_continue=True, auto_extend_segments=True),
                      client=client, approve=lambda name, args: approvals.append(name) == 99)
        with patch("termux_agent.agent.load_prompt_file", return_value="bounded automatic rules"):
            agent.run("write both files")
        self.assertEqual(approvals, ["write_file"])
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())
        self.assertEqual(agent.last_run["stop_reason"], "approval_denied")
        stats = agent.stats()
        self.assertEqual(stats["tool_calls_executed"], 0)
        self.assertEqual(stats["tool_calls_denied"], 1)
        self.assertEqual(stats["tool_calls_not_run"], 1)
        outcomes = [json.loads(m["content"]) for m in agent.messages if m["role"] == "tool"]
        self.assertEqual([item.get("execution_status") for item in outcomes], ["denied", "not_run"])

    def test_identical_call_and_result_across_one_call_segments_stops_at_threshold(self):
        source = self.root / "same.txt"
        source.write_text("same", encoding="utf-8")
        replies = []
        for index in range(10):
            identifier = f"same{index}"
            replies.append({"tool_calls": [call(identifier, "read_file",
                                                 {"path": str(source), "offset": 0, "limit": 4})]})
        client = ScriptedClient(replies)
        agent = Agent(self.config(max_steps=1, max_task_calls=10, auto_continue=True,
                                  auto_extend_segments=True, no_progress_detection=True,
                                  no_progress_threshold=3), client=client)
        with patch("termux_agent.agent.load_prompt_file", return_value="bounded automatic rules"):
            agent.run("repeat the same read")
        self.assertEqual(len(client.sent), 3)
        self.assertEqual(agent.last_run["stop_reason"], "no_progress")
        self.assertGreaterEqual(agent.task_state["segments"], 3)


class CliCompactionTests(AcceptanceTestCase):
    def make_tool_chain(self, count=5):
        source = self.root / "chain-source.txt"
        source.write_text("abcde", encoding="utf-8")
        calls = [call(f"chain{i}", "read_file",
                      {"path": str(source), "offset": i, "limit": 1, "note": "N" * 900})
                 for i in range(count)]
        messages = [{"role": "system", "content": "system instructions retained"},
                    {"role": "user", "content": "G" * 1500 + " original task goal"}]
        for index, current in enumerate(calls):
            opaque = {"type": "reasoning", "encrypted_content": "OPAQUE" * 200}
            assistant = {"role": "assistant", "content": f"step {index}",
                         "tool_calls": [current], "reasoning_content": "R" * 500,
                         "_responses_output": [copy.deepcopy(opaque)]}
            messages.extend([assistant, tool_result(current["id"], {"content": "ok", "offset": index})])
        return messages

    def test_real_cli_auto_compaction_archives_and_branches_without_rewriting_original(self):
        config_path = self.root / "auto-compact.toml"
        config_path.write_text('base_url="http://127.0.0.1:9/v1"\nmodel="acceptance-model"\n'
                               'approval_policy="never"\nstream=false\nmax_steps=6\n'
                               'max_task_calls=20\nmax_context_chars=100000\n', encoding="utf-8")
        store_root = self.root / "store"
        initial_calls = [
            call(f"pre{i}", "read_file", {"path": str(config_path), "offset": i, "limit": 1})
            for i in range(5)
        ]
        replies = [
            *({"tool_calls": [current], "reasoning_content": "R" * 10000}
              for current in initial_calls[:3]),
            *({"tool_calls": [current]} for current in initial_calls[3:]),
            {"content": "first segment complete"},
        ]
        self.run_cli(["--config", str(config_path), "--directory", str(self.root), "--no-agent"],
                     ["start persistent task", "/exit"], replies, store=store_root)
        with open_store(store_root) as store:
            original_id = store.ids()[-1]
            row_before = store.db.execute("SELECT title,updated,revision,payload FROM sessions WHERE id=?",
                                          (original_id,)).fetchone()
        original_payload = json.loads(row_before[3])
        source_context_chars = sum(
            len(json.dumps(message, ensure_ascii=False, separators=(",", ":")))
            for message in original_payload["messages"]
        )
        self.assertGreater(source_context_chars, 30000)

        config_path.write_text('base_url="http://127.0.0.1:9/v1"\nmodel="acceptance-model"\n'
                               'approval_policy="never"\nstream=false\nmax_steps=2\n'
                               'max_task_calls=20\nmax_context_chars=20000\n'
                               'auto_continue=true\nauto_extend_segments=true\n'
                               'auto_compact_task=true\nauto_compact_keep_groups=2\n'
                               'auto_compact_threshold=0.85\n', encoding="utf-8")
        replies = [{"tool_calls": [call("aftercompact", "read_file",
                                          {"path": str(config_path), "offset": 0, "limit": 1})]},
                   {"content": "continued after compaction [TASK_DONE]"}]
        _, followup_requests = self.run_cli(
            ["--session", original_id], ["/continue 2", "/exit"], replies, store=store_root)
        request_bodies = [json.loads(line) for line in
                          followup_requests.read_text(encoding="utf-8").splitlines()]
        first_compacted_request = request_bodies[0]
        first_compacted_request_chars = sum(
            len(json.dumps(message, ensure_ascii=False, separators=(",", ":")))
            for message in first_compacted_request["messages"]
        ) + len(json.dumps(first_compacted_request["tools"], ensure_ascii=False,
                           separators=(",", ":")))
        self.assertLess(first_compacted_request_chars, 20000)
        with open_store(store_root) as store:
            row_after = store.db.execute("SELECT title,updated,revision,payload FROM sessions WHERE id=?",
                                         (original_id,)).fetchone()
            self.assertEqual(row_after, row_before)
            new_ids = [identifier for identifier in store.ids() if identifier != original_id]
            self.assertEqual(len(new_ids), 1)
            compacted = store.load(new_ids[0])[1]
        self.assertLess(len(compacted["messages"]), len(original_payload["messages"]))
        compacted_by_id = {message.get("tool_calls", [{}])[0].get("id"): message
                           for message in compacted["messages"] if message.get("tool_calls")}
        original_by_id = {message.get("tool_calls", [{}])[0].get("id"): message
                          for message in original_payload["messages"] if message.get("tool_calls")}
        for retained_id in ("pre3", "pre4"):
            self.assertEqual(compacted_by_id[retained_id], original_by_id[retained_id])
        memos = [message for message in compacted["messages"]
                 if str(message.get("content", "")).startswith("[Task memo:")]
        self.assertTrue(any(str(message.get("content", "")).startswith("[Task memo:")
                            for message in compacted["messages"]))
        self.assertEqual(len(memos), 1)

    def test_external_update_during_pre_accept_recovery_is_never_rolled_back(self):
        config_path = self.root / "external-recovery.toml"
        config_path.write_text('base_url="http://127.0.0.1:9/v1"\nmodel="acceptance-model"\n'
                               'approval_policy="never"\nstream=false\ntoken_count_mode="off"\n'
                               'max_steps=2\nmax_task_calls=20\nmax_context_chars=100000\n'
                               'context_recovery=true\nauto_compact_task=true\n'
                               'auto_compact_keep_groups=1\nauto_compact_threshold=0.95\n',
                               encoding="utf-8")
        cfg = Config.load(config_path)
        agent = Agent(cfg, client=ScriptedClient([]), instructions="preserve external source data")
        agent.messages[:] = self.make_tool_chain(5)
        scope = {"directory": str(self.root), "config_path": str(config_path),
                 "instructions": "preserve external source data", "session_file": None,
                 "disabled": False, "instruction_path": None}
        identifier = "abcdef123456"
        store_root = self.root / "store"
        with open_store(store_root) as store:
            store.save(identifier, 0, snapshot(agent, scope))
            store.rename(identifier, "Original archive title")
            before = store.db.execute("SELECT title,updated,revision,payload FROM sessions WHERE id=?",
                                      (identifier,)).fetchone()

        record = self.root / "external-row.json"
        update = {"id": identifier, "title": "External source title",
                  "content": "EXTERNAL DATA MUST SURVIVE", "record": str(record)}
        responses = [
            {"kind": "context", "text": "pre-accept context", "pre_accept": True,
             "external_update": update},
            {"content": "recovered"},
        ]
        output, _ = self.run_cli(["--session", identifier],
                                 ["/continue 2", "/exit"],
                                 responses, store=store_root)

        self.assertTrue(record.is_file())
        external_row = tuple(json.loads(record.read_text(encoding="utf-8")))
        with open_store(store_root) as store:
            after = store.db.execute("SELECT title,updated,revision,payload FROM sessions WHERE id=?",
                                     (identifier,)).fetchone()
            new_ids = [item for item in store.ids() if item != identifier]
            self.assertEqual(after, external_row)
            self.assertGreaterEqual(after[1], before[1])
            self.assertGreaterEqual(after[2], before[2] + 1)
            self.assertEqual(after[0], "External source title")
            self.assertIn("EXTERNAL DATA MUST SURVIVE", after[3])
            if new_ids:
                branches = [store.load(item)[1] for item in new_ids]
                self.assertTrue(any(
                    any(str(message.get("content", "")).startswith("[Task memo:")
                        for message in branch["messages"])
                    for branch in branches))
            else:
                self.assertRegex(output, r"Session changed/deleted in another process|use /session fork")

    def test_explicit_compact_task_preserves_raw_retained_groups_and_second_call_is_noop(self):
        config_path = self.root / "explicit.toml"
        config_path.write_text('base_url="http://127.0.0.1:9/v1"\nmodel="acceptance-model"\n'
                               'token_count_mode="off"\n', encoding="utf-8")
        messages = self.make_tool_chain(5)
        cfg = Config.load(config_path)
        agent = Agent(cfg, client=ScriptedClient([]), instructions="instruction kept verbatim")
        agent.messages[:] = messages
        scope = {"directory": str(self.root), "config_path": str(config_path),
                 "instructions": "instruction kept verbatim", "session_file": None,
                 "disabled": False, "instruction_path": None}
        identifier = "abcdef123456"
        store_root = self.root / "store"
        with open_store(store_root) as store:
            store.save(identifier, 0, snapshot(agent, scope))
            store.rename(identifier, "Original archive title")
            before = store.db.execute("SELECT title,updated,revision,payload FROM sessions WHERE id=?",
                                      (identifier,)).fetchone()

        output, _ = self.run_cli(["--session", identifier],
                                 ["/compact task 2", "/compact task 2", "/exit"],
                                 store=store_root)
        self.assertTrue("omitted_groups" in output or "reduced_calls" in output)
        with open_store(store_root) as store:
            after = store.db.execute("SELECT title,updated,revision,payload FROM sessions WHERE id=?",
                                     (identifier,)).fetchone()
            self.assertEqual(after, before)
            self.assertEqual(len(store.ids()), 2)
            compacted = next(store.load(item)[1] for item in store.ids() if item != identifier)

        original_payload = json.loads(before[3])
        self.assertEqual(compacted["messages"][0], original_payload["messages"][0])
        self.assertEqual(compacted["messages"][1], original_payload["messages"][1])
        self.assertEqual(sum(str(m.get("content", "")).startswith("[Task memo:")
                             for m in compacted["messages"]), 1)
        original_by_id = {m.get("tool_calls", [{}])[0].get("id"): m
                          for m in original_payload["messages"] if m.get("tool_calls")}
        compact_by_id = {m.get("tool_calls", [{}])[0].get("id"): m
                         for m in compacted["messages"] if m.get("tool_calls")}
        self.assertEqual(set(compact_by_id), {"chain3", "chain4"})
        for identifier_value in compact_by_id:
            self.assertEqual(compact_by_id[identifier_value], original_by_id[identifier_value])


class TokenBudgetTests(AcceptanceTestCase):
    def test_native_metadata_roots_and_slots_arrays_are_supported(self):
        for base_url in ("http://127.0.0.1:8080/v1", "http://127.0.0.1:8080/prefix/v1"):
            with self.subTest(base_url=base_url):
                seen = []
                def respond(request):
                    path = request.url.path
                    seen.append(str(request.url))
                    if path.endswith("/props"):
                        return httpx.Response(200, json={})
                    if path.endswith("/slots"):
                        return httpx.Response(200, json=[{"n_ctx": 4096}])
                    if path.endswith("/apply-template"):
                        return httpx.Response(200, json={"prompt": "rendered"})
                    if path.endswith("/tokenize"):
                        return httpx.Response(200, json={"tokens": [1, 2, 3]})
                    raise AssertionError(path)
                cfg = self.config(base_url=base_url, api_kind="local", token_count_mode="local",
                                  context_window_tokens=0, output_reserve_tokens=32)
                http = httpx.Client(transport=httpx.MockTransport(respond))
                counter = TokenCounter(cfg, http_client=http,
                                       cache_path=self.root / ("cache-" + str(len(seen)) + ".json"))
                result = counter.measure([{"role": "user", "content": "hello"}])
                self.assertEqual(result["context_window_tokens"], 4096)
                self.assertIsNotNone(result["prompt_tokens"])
                root = base_url[:-3]
                self.assertEqual(seen[:2], [root + "/props", root + "/slots"])
                self.assertIn(root + "/apply-template", seen)
                self.assertIn(root + "/tokenize", seen)
                http.close()

    def test_manual_window_counts_when_metadata_and_template_are_unavailable_and_failures_are_cached(self):
        counts = {"props": 0, "template": 0, "tokenize": 0}
        def respond(request):
            path = request.url.path
            if path.endswith("/props"):
                counts["props"] += 1
                return httpx.Response(503, text="unavailable")
            if path.endswith("/apply-template"):
                counts["template"] += 1
                return httpx.Response(503, text="unsupported")
            if path.endswith("/tokenize"):
                counts["tokenize"] += 1
                return httpx.Response(200, json={"tokens": [1, 2, 3, 4]})
            raise AssertionError(path)
        cfg = self.config(api_kind="local", token_count_mode="local",
                          context_window_tokens=8192, output_reserve_tokens=32)
        http = httpx.Client(transport=httpx.MockTransport(respond))
        counter = TokenCounter(cfg, http_client=http,
                               cache_path=self.root / "fallback-cache.json")
        first = counter.measure([{"role": "user", "content": "count this"}])
        second = counter.measure([{"role": "user", "content": "count again"}])
        self.assertIsNotNone(first["prompt_tokens"])
        self.assertIsNotNone(second["prompt_tokens"])
        self.assertEqual(first["context_window_tokens"], 8192)
        self.assertFalse(first["exact"])
        self.assertNotEqual(first["source"], "llama_cpp")
        self.assertEqual(counts, {"props": 1, "template": 1, "tokenize": 2})
        http.close()

    def test_identifier_and_unicode_heuristics_do_not_undercount(self):
        identifier = "prefix_" + "a" * 12000
        unicode_text = "reasoning " + "\u6d4b\u8bd5" * 300 + " " + "!" * 400
        self.assertGreater(estimate_text_tokens(identifier), 1000)
        self.assertGreater(estimate_text_tokens(unicode_text), 300)
        result = TokenCounter(self.config(token_count_mode="estimate"),
                              cache_path=self.root / "estimate.json").measure(
                                  [{"role": "user", "content": identifier}])
        self.assertFalse(result["exact"])
        self.assertGreater(result["prompt_tokens"], 1000)


class HistoryPrivacyTests(AcceptanceTestCase):
    def test_history_only_stats_status_history_and_export_make_no_network_clients(self):
        config_path = self.root / "history.toml"
        config_path.write_text('base_url="http://old.example/v1"\nmodel="old-model"\n'
                               'token_count_mode="local"\napi_key_env=""\n', encoding="utf-8")
        cfg = Config.load(config_path)
        agent = Agent(cfg, client=ScriptedClient([]))
        agent.messages.extend([{"role": "user", "content": "archived question"},
                               {"role": "assistant", "content": "archived answer"}])
        scope = {"directory": str(self.root), "config_path": str(config_path),
                 "instructions": "saved", "session_file": None, "disabled": False,
                 "instruction_path": None}
        identifier = "abcdef123456"
        with open_store(self.root / "store") as store:
            payload = snapshot(agent, scope)
            payload["config"]["token_count_mode"] = "local"
            payload["config"]["api_kind"] = "local"
            store.save(identifier, 0, payload)
            before = store.db.execute("SELECT title,updated,revision,payload FROM sessions WHERE id=?",
                                      (identifier,)).fetchone()
        config_path.write_text('base_url="http://new.example/v1"\nmodel="new-model"\n', encoding="utf-8")
        export = self.root / "history.json"
        output, requests = self.run_cli(
            ["--session", identifier],
            ["/stats", "/status", "/history", f"/export json {export}", "/exit"],
            store=self.root / "store", bomb_network=True, bomb_agent_client=True)
        self.assertIn("archived answer", output)
        self.assertTrue(export.is_file())
        self.assertEqual(json.loads(export.read_text(encoding="utf-8"))["payload"]["messages"][2]["content"],
                         "archived answer")
        self.assertEqual(requests.read_text(encoding="utf-8"), "")
        with open_store(self.root / "store") as store:
            self.assertEqual(store.db.execute("SELECT title,updated,revision,payload FROM sessions WHERE id=?",
                                              (identifier,)).fetchone(), before)


class IncompleteOutputTests(AcceptanceTestCase):
    def test_length_output_is_draft_only_and_never_replays_incomplete_calls(self):
        dangling = {"type": "function_call", "id": "provider-id", "call_id": "dangling",
                    "name": "write_file", "arguments": '{"path":"must-not-exist","content":"bad"}'}
        incomplete = {
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [{"type": "reasoning", "encrypted_content": "PRIVATE_REASONING"},
                       dangling, text_item("partial draft")],
            "usage": {"input_tokens": 11, "output_tokens": 7},
        }
        requests = []
        def respond(request):
            requests.append(json.loads(request.content))
            if len(requests) == 1:
                return httpx.Response(200, json=incomplete)
            return httpx.Response(200, json={"status": "completed", "output": [text_item("next answer")]})
        config_path = self.root / "responses.toml"
        config_path.write_text('base_url="http://127.0.0.1:9/v1"\nmodel="acceptance-model"\n'
                               'wire_api="responses"\nmax_output_tokens=256\n'
                               'output_reserve_tokens=256\nmax_task_calls=10\n', encoding="utf-8")
        cfg = self.config(wire_api="responses", max_output_tokens=256,
                          output_reserve_tokens=256, max_task_calls=10)
        transport = httpx.MockTransport(respond)
        agent = Agent(cfg, client=Client(cfg, http_client=httpx.Client(transport=transport)))
        execute = Mock()
        with patch("termux_agent.agent.execute", execute):
            result = agent.run("write a file")
        self.assertEqual(agent.last_run["stop_reason"], "output_limit")
        self.assertTrue(result)
        execute.assert_not_called()
        self.assertIsNotNone(agent.incomplete_draft)
        self.assertIn("partial draft", agent.incomplete_draft["content"])
        self.assertIn("_incomplete_tool_calls", agent.incomplete_draft)
        self.assertFalse(any("_incomplete" in json.dumps(message) or
                             "dangling" in json.dumps(message)
                             for message in agent.messages))

        store_root = self.root / "store"
        identifier = "abcdef123456"
        scope = {"directory": str(self.root), "config_path": str(config_path), "instructions": "",
                 "session_file": None, "disabled": False, "instruction_path": None}
        with open_store(store_root) as store:
            store.save(identifier, 0, snapshot(agent, scope))
            revision, payload = store.load(identifier)
        self.assertEqual(payload["incomplete_draft"]["content"], "partial draft")
        restored_client = Client(cfg, http_client=httpx.Client(transport=transport))
        def build(config, text, base_system=None, client=None):
            return Agent(config, client=client or restored_client, instructions=text,
                         base_system=base_system)
        _, restored, _ = restore(payload, build)
        self.assertEqual(restored.incomplete_draft["content"], "partial draft")
        self.assertEqual(restored.run("continue with a clean request"), "next answer")
        wire = json.dumps(requests[-1], ensure_ascii=False)
        self.assertNotIn("dangling", wire)
        self.assertNotIn("_incomplete", wire)
        self.assertNotIn("partial draft", wire)
        self.assertNotIn("PRIVATE_REASONING", wire)


class RecoveryAccountingTests(AcceptanceTestCase):
    def test_one_pre_accept_recovery_counts_and_hard_budget_prevents_it(self):
        source = self.root / "recovery.txt"
        source.write_text("abcdef", encoding="utf-8")
        specs = [
            {"tool_calls": [call("r1", "read_file", {"path": str(source), "offset": 0, "limit": 1,
                                                        "note": "N" * 3000})],
             "_usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
            {"tool_calls": [call("r2", "read_file", {"path": str(source), "offset": 1, "limit": 1,
                                                        "note": "N" * 3000})],
             "_usage": {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4}},
            {"_raise": ContextLengthError("pre-accept context", pre_accept=True),
             "_usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
            {"content": "recovered", "_usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6}},
        ]
        client = ScriptedClient(specs)
        agent = Agent(self.config(max_steps=4, max_task_calls=4, context_recovery=True,
                                  auto_compact_keep_groups=1), client=client)
        self.assertEqual(agent.run("use recovery once"), "recovered")
        self.assertEqual(len(client.sent), 4)
        self.assertEqual(agent.stats()["model_logical_calls_total"], 4)
        self.assertEqual(agent.task_state["logical_calls"], 4)
        self.assertEqual(agent.task_state["recovery_requests"], 1)
        usage = agent.stats()["usage_totals"]
        self.assertEqual((usage["prompt_tokens"], usage["completion_tokens"], usage["total_tokens"]),
                         (18, 9, 27))

        denied_client = ScriptedClient([
            {"tool_calls": [call("b1", "read_file", {"path": str(source), "offset": 0, "limit": 1})],
             "_usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
            {"_raise": ContextLengthError("pre-accept context", pre_accept=True),
             "_usage": {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4}},
        ])
        blocked = Agent(self.config(max_steps=3, max_task_calls=2, context_recovery=True,
                                    auto_compact_keep_groups=1), client=denied_client)
        blocked.run("must not recover past budget")
        self.assertEqual(len(denied_client.sent), 2)
        self.assertEqual(blocked.task_state["logical_calls"], 2)
        self.assertEqual(blocked.task_state["recovery_requests"], 0)

    def test_accepted_partial_stream_is_never_retried(self):
        class BrokenStream:
            status_code = 200
            headers = {"content-type": "text/event-stream"}
            def iter_lines(self):
                yield 'data: {"choices":[{"delta":{"content":"accepted partial"}}]}'
                raise httpx.ReadError("stream cut after acceptance")
            def close(self):
                pass
            def raise_for_status(self):
                pass
        sends = []
        class BrokenHttp:
            def build_request(self, method, url, **kwargs):
                return httpx.Request(method, url)
            def send(self, request, stream=False):
                sends.append(request)
                return BrokenStream()
            def close(self):
                pass
        cfg = self.config(stream=True, retry_max_attempts=3)
        client = Client(cfg, http_client=BrokenHttp())
        with self.assertRaises(NetworkError):
            client.chat([{"role": "user", "content": "partial"}], [])
        self.assertEqual(len(sends), 1)
        self.assertEqual(client.last_attempts, 1)


class StateAndTimingTests(AcceptanceTestCase):
    def test_corrupt_optional_task_state_does_not_block_history_viewing(self):
        config_path = self.root / "corrupt.toml"
        config_path.write_text('base_url="http://old.example/v1"\nmodel="old-model"\n', encoding="utf-8")
        agent = Agent(Config.load(config_path), client=ScriptedClient([]))
        agent.messages.extend([{"role": "user", "content": "visible history"},
                               {"role": "assistant", "content": "visible answer"}])
        scope = {"directory": str(self.root), "config_path": str(config_path),
                 "instructions": "", "session_file": None, "disabled": False, "instruction_path": None}
        payload = snapshot(agent, scope)
        payload["task_state"] = {"logical_calls": "bad", "segments": []}
        payload["last_run"] = ["not", "a", "mapping"]
        payload["incomplete_draft"] = 12345
        config_path.write_text('base_url="http://new.example/v1"\nmodel="new-model"\n', encoding="utf-8")
        _, restored, _ = restore(payload, lambda cfg, text, base_system=None, client=None:
                                 Agent(cfg, client=client or ScriptedClient([]), instructions=text,
                                       base_system=base_system), history_fallback=True)
        self.assertTrue(restored.history_only)
        self.assertEqual(restored.messages[-1]["content"], "visible answer")
        self.assertIsInstance(restored.stats(), dict)

    def test_time_is_not_double_counted_and_limits_are_explicit(self):
        client = ScriptedClient([{"content": "done"}])
        agent = Agent(self.config(max_task_seconds=1, timeout=0.5, max_output_tokens=100,
                                  output_reserve_tokens=100), client=client)
        values = iter((100.0, 105.0, 105.0))
        with patch("termux_agent.agent.time.monotonic", side_effect=lambda: next(values, 105.0)):
            agent.run("single bounded action")
        self.assertLessEqual(agent.last_run["task_budget"]["active_seconds"], 5.01)
        self.assertLessEqual(agent.task_state["active_seconds"], 5.01)

        cfg = self.config(stream=False, max_output_tokens=100, output_reserve_tokens=100)
        captured = []
        def respond(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"},
                                                           "finish_reason": "stop"}]})
        client = Client(cfg, http_client=httpx.Client(transport=httpx.MockTransport(respond)))
        client.chat([{"role": "user", "content": "bounded"}], [])
        self.assertEqual(captured[0]["max_completion_tokens"], 100)
        self.assertLessEqual(cfg.timeout, cfg.max_task_seconds)
        with self.assertRaises(ValueError):
            self.config(max_output_tokens=200, output_reserve_tokens=100).validate()


class CliSurfaceTests(AcceptanceTestCase):
    def test_help_and_status_expose_continue_compaction_and_stop_progress(self):
        from termux_agent.terminal_ui import COMMANDS, SUBCOMMANDS
        self.assertIn("/continue", COMMANDS)
        self.assertIn("task", SUBCOMMANDS["/compact"])
        responses = [{"content": "working", "_finish_reason": "stop"}]
        output, _ = self.run_cli(["--no-session", "--no-agent"],
                                 ["/help", "/status", "/exit"], responses)
        self.assertIn("/continue [N]", output)
        self.assertIn("/compact task [N]", output)
        self.assertIn("stop_reason", output)


if __name__ == "__main__":
    unittest.main()
