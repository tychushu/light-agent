"""Focused regression coverage for optional RTK shell-output compression."""

from __future__ import annotations

import base64
import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from termux_agent.agent import Agent
from termux_agent.config import Config
from termux_agent.output_compression import (
    RAW_STREAM_BYTES,
    OutputCompressor,
    classify_shell_command,
    redact_bytes,
    resolve_rtk_binary,
    run_rtk_filter,
    save_raw_artifact,
    serialized_result_bytes,
    stable_result_payload,
)
from termux_agent.sessions import SessionStore, restore, snapshot
from termux_agent.tools import execute


FAKE_PYTEST = r'''#!/usr/bin/env python3
import os
import pathlib
import sys
import time

mode = os.environ.get("RTK_TEST_MODE", "verbose")
count = os.environ.get("RTK_TEST_COUNT_FILE")
if count:
    with open(count, "a", encoding="utf-8") as stream:
        stream.write("run\n")

if mode == "quiet":
    sys.stdout.write("one passed\n")
elif mode == "nul":
    sys.stdout.buffer.write(b"machine\0output\n")
elif mode == "large":
    sys.stdout.buffer.write(b"x" * (int(os.environ.get("RTK_TEST_LARGE_BYTES", "600000"))))
elif mode == "split-secret":
    secret = os.environ.get("TERMUX_AGENT_API_KEY", "")
    middle = len(secret) // 2
    sys.stdout.write("HEAD_UNICODE=开头\n")
    sys.stdout.write(secret[:middle])
    sys.stdout.flush()
    time.sleep(0.03)
    sys.stdout.write(secret[middle:])
    sys.stdout.write("\nLONG_MIDDLE_ERROR_MARKER\n")
    sys.stdout.write("detail-é中\n" * 300)
    sys.stdout.write("TAIL_UNICODE=结尾\n")
else:
    sys.stdout.write("[START]\n")
    sys.stdout.write(("verbose test line " + "x" * 80 + "\n") * 80)
    sys.stdout.write("LONG_MIDDLE_ERROR_MARKER\n")
    sys.stdout.write("TAIL_UNICODE=结尾\n")

if os.environ.get("RTK_TEST_STDERR"):
    sys.stderr.write(os.environ["RTK_TEST_STDERR"] + "\n")
raise SystemExit(int(os.environ.get("RTK_TEST_EXIT", "0")))
'''


FAKE_RTK = r'''#!/usr/bin/env python3
import os
import subprocess
import sys
import time

data = sys.stdin.buffer.read()
mode = os.environ.get("RTK_FILTER_MODE", "ok")
if mode == "fail":
    sys.stderr.write("fake filter failure\n")
    raise SystemExit(9)
if mode == "child":
    subprocess.Popen([sys.executable, "-c", "import os,time; time.sleep(1); open(os.environ['RTK_CHILD_MARKER'],'w').write('child')"])
    time.sleep(30)
if mode == "timeout":
    time.sleep(30)
if mode == "empty":
    raise SystemExit(0)
if mode == "identity":
    sys.stdout.buffer.write(data)
else:
    sys.stdout.write("300 passed in 1.00s\n")
'''


class StubClient:
    history_only = False
    last_usage = last_finish_reason = None
    last_attempts = 0

    def chat(self, *args, **kwargs):
        return {"content": "done"}

    def close(self):
        pass


class ShellClient:
    history_only = False
    last_usage = last_finish_reason = None
    last_attempts = 0

    def __init__(self, command="pytest", max_steps=2):
        self.command = command
        self.max_steps = max_steps
        self.calls = 0

    def chat(self, request, tools):
        self.calls += 1
        if self.calls < self.max_steps:
            return {"content": "", "tool_calls": [{"id": f"call-{self.calls}", "type": "function",
                                                    "function": {"name": "shell", "arguments": json.dumps(
                                                        {"command": self.command})}}]}
        return {"content": "child complete"}

    def close(self):
        pass


class RTKFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.pytest = self.bin_dir / "pytest"
        self.rtk = self.bin_dir / "rtk-fixture"
        self.pytest.write_text(FAKE_PYTEST, encoding="utf-8")
        self.rtk.write_text(FAKE_RTK, encoding="utf-8")
        self.pytest.chmod(0o755)
        self.rtk.chmod(0o755)
        self.count_file = self.root / "command-count"
        self.artifact_root = self.root / "data"
        self.secret = "split-secret-value-0123456789"
        env = {
            "HOME": str(self.root / "home"),
            "PATH": str(self.bin_dir) + os.pathsep + os.environ.get("PATH", ""),
            "TERMUX_AGENT_RTK": str(self.rtk),
            "TERMUX_AGENT_API_KEY": self.secret,
            "RTK_FILTER_MODE": "ok",
            "RTK_TEST_MODE": "verbose",
            "RTK_TEST_COUNT_FILE": str(self.count_file),
        }
        self.env_patch = patch.dict(os.environ, env, clear=False)
        self.env_patch.start()
        self.config = Config(rtk_enabled=True, rtk_binary=str(self.rtk),
                             approval_policy="never", max_output_chars=20000)
        self.compressor = OutputCompressor(self.config, artifact_root=self.artifact_root)

    def tearDown(self):
        self.env_patch.stop()
        self.temp.cleanup()

    def compressor_for(self, **changes):
        config = Config(**{**self.config.__dict__, **changes})
        config.validate()
        return OutputCompressor(config, artifact_root=self.artifact_root)

    def read_archive(self, result):
        reference = result["output_compression"]["raw_artifact"]
        raw = execute("read_file", {"path": reference["path"], "offset": 0, "limit": 500000},
                      max_output_chars=500000)
        self.assertNotIn("error", raw, raw)
        payload = json.loads(raw["content"])
        if "stdout_text" in payload or "stderr_text" in payload:
            stdout = payload.get("stdout_text", "").encode("utf-8")
            stderr = payload.get("stderr_text", "").encode("utf-8")
        else:
            stdout = base64.b64decode(payload["stdout_base64"])
            stderr = base64.b64decode(payload["stderr_base64"])
        return payload, (stdout, stderr)

    def test_config_defaults_types_and_profile_overrides(self):
        default = Config()
        self.assertFalse(default.rtk_enabled)
        self.assertEqual(default.rtk_binary, "")
        for assignment in ('rtk_enabled = "true"', "rtk_binary = 3"):
            path = self.root / "invalid.toml"
            path.write_text(assignment + "\n", encoding="utf-8")
            with self.subTest(assignment=assignment), self.assertRaises(ValueError):
                Config.load(path)

        path = self.root / "profiles.toml"
        path.write_text(
            'default_api = "local"\nrtk_enabled = true\nrtk_binary = "~/global/rtk"\n'
            '[apis.local]\nbase_url = "http://127.0.0.1:1/v1"\nmodel = "local"\n'
            'rtk_binary = "~/local/rtk"\n'
            '[apis.cloud]\nbase_url = "https://example.invalid/v1"\nmodel = "cloud"\n'
            'rtk_enabled = false\n', encoding="utf-8")
        self.assertTrue(Config.load(path, "default").rtk_enabled)
        self.assertEqual(Config.load(path, "local").rtk_binary, "~/local/rtk")
        self.assertFalse(Config.load(path, "cloud").rtk_enabled)

    def test_binary_resolution_supports_config_env_and_path(self):
        home_bin = self.root / "home" / "custom"
        home_bin.parent.mkdir(parents=True)
        home_bin.write_text("fixture", encoding="utf-8")
        home_bin.chmod(0o755)
        with patch.dict(os.environ, {"HOME": str(self.root / "home"),
                                    "TERMUX_AGENT_RTK": ""}, clear=False):
            self.assertEqual(resolve_rtk_binary("~/custom"), str(home_bin))
        with patch.dict(os.environ, {"TERMUX_AGENT_RTK": str(self.rtk)}, clear=False):
            self.assertEqual(resolve_rtk_binary(""), str(self.rtk))
        path_rtk = self.bin_dir / "rtk"
        path_rtk.write_text("fixture", encoding="utf-8")
        path_rtk.chmod(0o755)
        with patch.dict(os.environ, {"TERMUX_AGENT_RTK": "", "PATH": str(self.bin_dir)}, clear=False):
            self.assertEqual(resolve_rtk_binary(""), str(path_rtk))

    def test_off_missing_and_unsupported_are_exact_pass_through(self):
        command = "pytest"
        disabled = execute("shell", {"command": command}, 20000)
        with patch("termux_agent.output_compression.resolve_rtk_binary", return_value=""):
            missing = execute("shell", {"command": command}, 20000,
                              output_compressor=self.compressor)
        self.assertEqual(disabled, missing)
        self.assertNotIn("output_compression", missing)

        unsupported_command = 'printf "raw\\n"'
        plain = execute("shell", {"command": unsupported_command}, 20000)
        unsupported = execute("shell", {"command": unsupported_command}, 20000,
                              output_compressor=self.compressor)
        self.assertEqual(plain, unsupported)
        self.assertNotIn("output_compression", unsupported)

    def test_disabled_does_not_scan_for_rtk(self):
        with patch("termux_agent.output_compression.resolve_rtk_binary",
                   side_effect=AssertionError("unexpected RTK lookup")):
            result = execute("shell", {"command": "pytest"}, 20000,
                             output_compressor=OutputCompressor(Config(rtk_enabled=False)))
        self.assertNotIn("output_compression", result)

    def test_classification_is_conservative(self):
        self.assertEqual(classify_shell_command("git status"), "git-status")
        self.assertEqual(classify_shell_command("git log -n 10"), "git-log")
        self.assertEqual(classify_shell_command("rg -n pattern"), "rg")
        self.assertEqual(classify_shell_command("grep -n pattern file"), "grep")
        self.assertEqual(classify_shell_command("python3 -m pytest tests"), "pytest")
        for command in ("git status --porcelain", "git status --short", "git log --format=%h",
                        "rg --json pattern", "pytest --junitxml=results.xml",
                        "python -m unittest discover", "printf raw", "git status | cat",
                        "git status; true", "git status > out"):
            with self.subTest(command=command):
                self.assertIsNone(classify_shell_command(command))

    def test_verbose_pytest_reduces_without_changing_exit_or_stderr(self):
        with patch.dict(os.environ, {"RTK_TEST_EXIT": "7", "RTK_TEST_STDERR": "diagnostic-é中"}):
            result = execute("shell", {"command": "pytest"}, 20000,
                             output_compressor=self.compressor)
        metadata = result["output_compression"]
        self.assertEqual(result["exit_code"], 7)
        self.assertIn("diagnostic-é中", result["stderr"])
        self.assertEqual(metadata["status"], "compressed")
        self.assertTrue(metadata["applied"])
        self.assertGreater(metadata["before_bytes"], metadata["after_bytes"])
        self.assertGreater(self.compressor.last_stats["before_chars"],
                           self.compressor.last_stats["after_chars"])
        self.assertEqual(metadata["after_bytes"], len(serialized_result_bytes(result)))
        self.assertIn("300 passed", result["stdout"])

    def test_filter_failure_executes_once_and_raw_is_recoverable(self):
        with patch.dict(os.environ, {"RTK_FILTER_MODE": "fail"}):
            result = execute("shell", {"command": "pytest"}, 20000,
                             output_compressor=self.compressor)
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("LONG_MIDDLE_ERROR_MARKER", result["stdout"])
        self.assertEqual(result["output_compression"]["status"], "filter_failed")
        self.assertTrue(result["output_compression"]["raw_available"])
        self.assertEqual(self.count_file.read_text(encoding="utf-8").splitlines(), ["run"])
        payload, (stdout, stderr) = self.read_archive(result)
        self.assertEqual(payload["exit_code"], 0)
        self.assertIn(b"LONG_MIDDLE_ERROR_MARKER", stdout)
        self.assertEqual(stderr, b"")

    def test_filter_timeout_after_real_command_falls_back_and_archives(self):
        self.compressor.filter_timeout = 0.1
        with patch.dict(os.environ, {"RTK_FILTER_MODE": "timeout"}):
            result = execute("shell", {"command": "pytest"}, 20000,
                             output_compressor=self.compressor)
        self.assertNotIn("timed_out", result)
        self.assertIn("LONG_MIDDLE_ERROR_MARKER", result["stdout"])
        self.assertEqual(result["output_compression"]["status"], "filter_failed")
        self.assertEqual(self.count_file.read_text(encoding="utf-8").splitlines(), ["run"])
        payload, _ = self.read_archive(result)
        self.assertFalse(payload["timed_out"])

    def test_long_middle_unicode_and_split_secret_are_redacted(self):
        with patch.dict(os.environ, {"RTK_FILTER_MODE": "fail", "RTK_TEST_MODE": "split-secret"}):
            result = execute("shell", {"command": "pytest"}, 360,
                             output_compressor=self.compressor)
        visible = result["stdout"] + result["stderr"]
        self.assertIn("[TRUNCATED]", visible)
        self.assertIn("TAIL_UNICODE=结尾", visible)
        self.assertNotIn(self.secret, visible)
        _, (stdout, stderr) = self.read_archive(result)
        archive = stdout + stderr
        self.assertNotIn(self.secret.encode(), archive)
        self.assertIn(b"[REDACTED]", archive)
        self.assertIn("LONG_MIDDLE_ERROR_MARKER".encode(), archive)
        self.assertIn("TAIL_UNICODE=结尾".encode(), archive)

    def test_oversized_capture_is_bounded_and_not_called_complete(self):
        with patch.dict(os.environ, {"RTK_TEST_MODE": "large",
                                    "RTK_TEST_LARGE_BYTES": str(RAW_STREAM_BYTES + 10)}):
            result = execute("shell", {"command": "pytest"}, 1000,
                             output_compressor=self.compressor)
        metadata = result["output_compression"]
        self.assertEqual(metadata["status"], "oversized")
        self.assertFalse(metadata["raw_capture_complete"])
        self.assertFalse(metadata["raw_available"])
        self.assertNotIn("raw_artifact", metadata)
        self.assertEqual(metadata["before_bytes"], 0)
        self.assertEqual(metadata["after_bytes"], 0)
        self.assertLessEqual(len(result["stdout"]) + len(result["stderr"]), 1000)

    def test_no_benefit_and_machine_nul_output_pass_through(self):
        with patch.dict(os.environ, {"RTK_FILTER_MODE": "identity"}):
            result = execute("shell", {"command": "pytest"}, 20000,
                             output_compressor=self.compressor)
        self.assertNotIn("output_compression", result)
        self.assertEqual(self.compressor.last_stats["status"], "no_benefit")
        self.assertFalse(self.compressor.last_stats["applied"])
        self.assertEqual(self.compressor.last_stats["before_bytes"], 0)
        self.assertEqual(self.compressor.last_stats["after_bytes"], 0)
        self.assertIn("LONG_MIDDLE_ERROR_MARKER", result["stdout"])

        with patch.dict(os.environ, {"RTK_TEST_MODE": "nul"}):
            nul = execute("shell", {"command": "pytest"}, 20000,
                          output_compressor=self.compressor)
        self.assertIn("\0", nul["stdout"])
        self.assertNotIn("output_compression", nul)
        self.assertEqual(self.compressor.last_stats["status"], "unsupported_output")
        self.assertFalse(self.compressor.last_stats["filter_attempted"])

        with patch.dict(os.environ, {"RTK_FILTER_MODE": "empty"}):
            empty = execute("shell", {"command": "pytest"}, 20000,
                            output_compressor=self.compressor)
        self.assertEqual(empty["output_compression"]["status"], "filter_failed")
        self.assertTrue(empty["output_compression"]["filter_attempted"])
        self.assertIn("LONG_MIDDLE_ERROR_MARKER", empty["stdout"])

    def test_filter_timeout_and_interrupt_reap_process_groups(self):
        marker = self.root / "late-child"
        with patch.dict(os.environ, {"RTK_FILTER_MODE": "child", "RTK_CHILD_MARKER": str(marker)}):
            filtered = run_rtk_filter(str(self.rtk), "pytest", b"payload\n", timeout=0.1)
            self.assertTrue(filtered.timed_out)
            time.sleep(0.35)
            self.assertFalse(marker.exists())

            def interrupted_select(*args, **kwargs):
                time.sleep(0.15)
                raise KeyboardInterrupt

            with patch("termux_agent.output_compression.selectors.DefaultSelector.select",
                       side_effect=interrupted_select):
                with self.assertRaises(KeyboardInterrupt):
                    run_rtk_filter(str(self.rtk), "pytest", b"payload\n", timeout=5)
            time.sleep(0.35)
            self.assertFalse(marker.exists())

    def test_archive_permissions_quota_retention_and_symlink_refusal(self):
        directory = self.artifact_root / "output-artifacts"
        directory.mkdir(parents=True)
        user_file = directory / "user-owned.json"
        user_file.write_text("keep", encoding="utf-8")
        with patch("termux_agent.output_compression.ARTIFACT_RETENTION", 2), \
                patch("termux_agent.output_compression.ARTIFACT_GLOBAL_QUOTA", 9000):
            rows = [save_raw_artifact(b"a" * 2000, b"b", index, False,
                                      root=self.artifact_root) for index in range(3)]
        self.assertTrue(all(rows))
        generated = [item for item in directory.iterdir()
                     if item.name.startswith("shell-raw-")]
        self.assertLessEqual(len(generated), 2)
        self.assertTrue(user_file.exists())
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        for item in generated:
            self.assertEqual(stat.S_IMODE(item.stat().st_mode), 0o600)

        outside = self.root / "outside"
        outside.mkdir()
        symlink_root = self.root / "symlink-root"
        symlink_root.mkdir()
        (symlink_root / "output-artifacts").symlink_to(outside, target_is_directory=True)
        self.assertIsNone(save_raw_artifact(b"payload", b"", 0, False, root=symlink_root))
        self.assertEqual(list(outside.iterdir()), [])

    def test_file_tools_are_unchanged_by_compression_context(self):
        path = self.root / "file.txt"
        args = {"path": str(path), "content": "exact-file-content"}
        self.assertEqual(execute("write_file", args),
                         execute("write_file", args, output_compressor=self.compressor))
        read_args = {"path": str(path), "offset": 0, "limit": 100}
        self.assertEqual(execute("read_file", read_args),
                         execute("read_file", read_args, output_compressor=self.compressor))

    def test_child_inherits_settings_and_parent_merges_stats(self):
        child_client = ShellClient(max_steps=2)
        captured = []

        def child_factory(config):
            captured.append(config)
            return child_client

        parent = Agent(self.config, client=StubClient())
        with patch("termux_agent.agent.Client", side_effect=child_factory):
            result = parent._delegate({"task": "run tests", "max_steps": 2})
        self.assertEqual(result["status"], "complete")
        self.assertTrue(captured[0].rtk_enabled)
        self.assertEqual(captured[0].rtk_binary, str(self.rtk))
        self.assertGreaterEqual(parent.stats()["output_compression"]["compressed_results"], 1)

    def test_stats_sessions_restore_and_old_counter_defaults(self):
        config_path = self.root / "config.toml"
        config_path.write_text("rtk_enabled = true\nrtk_binary = %s\n" % json.dumps(str(self.rtk)),
                               encoding="utf-8")
        config = Config.load(config_path)
        agent = Agent(config, client=StubClient())
        agent._ensure_task_state("stats")
        calls = [{"id": "call-1", "type": "function", "function": {
            "name": "shell", "arguments": json.dumps({"command": "pytest"})}}]
        agent.messages.append({"role": "assistant", "content": "", "tool_calls": calls})
        agent._fill_tool_results(calls)
        scope = {"directory": str(self.root), "config_path": str(config_path),
                 "session_file": None, "disabled": False, "instructions": "",
                 "instruction_path": None}
        payload = snapshot(agent, scope)
        self.assertGreater(agent.stats()["output_compression"]["bytes_saved"], 0)
        self.assertIn("not actual tokenizer", agent.stats()["output_compression"]["unit"])
        self.assertIn("provider tokens", agent.stats()["output_compression"]["unit"])

        def build(next_config, instructions, **kwargs):
            return Agent(next_config, client=StubClient(), **kwargs)

        restored_config, restored, _ = restore(payload, build)
        self.assertTrue(restored_config.rtk_enabled)
        self.assertEqual(restored.stats()["output_compression"]["compressed_results"],
                         agent.stats()["output_compression"]["compressed_results"])

        old = json.loads(json.dumps(payload))
        for key in list(old["counters"]):
            if key.startswith("_rtk_"):
                old["counters"].pop(key)
        old["version"] = 4
        _, legacy, _ = restore(old, build)
        self.assertEqual(legacy.stats()["output_compression"]["compressed_results"], 0)

        store = SessionStore(self.root / "store")
        try:
            revision = store.save("abcdef123456", 0, payload)
            _, saved = store.load("abcdef123456")
            _, restored, _ = restore(saved, build)
            self.assertEqual(revision, 1)
            self.assertGreater(restored.stats()["output_compression"]["before_bytes"], 0)
        finally:
            store.close()

    def test_no_progress_ignores_unique_artifact_references(self):
        config = Config(**{**self.config.__dict__, "no_progress_detection": True,
                           "no_progress_threshold": 2})
        agent = Agent(config, client=StubClient())
        agent._ensure_task_state("repeat")
        results = []
        for index in range(2):
            calls = [{"id": f"same-{index}", "type": "function", "function": {
                "name": "shell", "arguments": json.dumps({"command": "pytest"})}}]
            agent.messages.append({"role": "assistant", "content": "", "tool_calls": calls})
            agent._fill_tool_results(calls)
            results.append(json.loads(agent.messages[-1]["content"]))
        self.assertTrue(agent._no_progress_stop)
        self.assertNotEqual(results[0]["output_compression"]["raw_artifact"]["id"],
                            results[1]["output_compression"]["raw_artifact"]["id"])
        self.assertEqual(stable_result_payload(results[0]), stable_result_payload(results[1]))


if __name__ == "__main__":
    unittest.main()
