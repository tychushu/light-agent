"""Focused capture, candidate and fallback regressions for RTK shell output."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from termux_agent.output_compression import (
    CompressionPlan,
    FilterResult,
    OutputCompressor,
    serialized_result_bytes,
)
from termux_agent.tools import execute


FAKE_COMMAND = r'''#!{python}
import os
import sys
import time

count = os.environ.get("CAPTURE_COUNT_FILE")
if count:
    with open(count, "a", encoding="utf-8") as stream:
        stream.write("run\n")
mode = os.environ.get("CAPTURE_MODE", "large")
if mode == "large":
    sys.stdout.write("A" * 50000)
    sys.stderr.write("CRITICAL_STDERR_SENTINEL")
    raise SystemExit(7)
if mode == "medium":
    sys.stdout.write("A" * 800)
    sys.stderr.write("diagnostic")
elif mode == "identity":
    sys.stdout.write("B" * 5000)
elif mode == "nul":
    sys.stdout.buffer.write(b"machine\0output")
elif mode == "secret":
    sys.stdout.write("S" * 40000 + os.environ.get("CAPTURE_SECRET", ""))
    sys.stderr.write("CRITICAL_STDERR_SENTINEL")
    raise SystemExit(7)
elif mode == "sleep":
    sys.stdout.write("A" * 500)
    sys.stdout.flush()
    time.sleep(2)
else:
    sys.stdout.write("small")
'''.format(python=sys.executable)


class CaptureCompressor(OutputCompressor):
    """Small policy double; archive/filter behavior is controlled per test."""

    def __init__(self, root: Path, save_mode: str = "ok", path_length: int = 0):
        super().__init__(SimpleNamespace(rtk_enabled=True, rtk_binary="rtk-fixture",
                                         api_key=""), artifact_root=root)
        self.save_mode = save_mode
        self.path_length = path_length
        self.save_calls = []

    def plan(self, command: str):
        self.last_stats = None
        return CompressionPlan("pytest", "rtk-fixture")

    def redact(self, data: bytes) -> bytes:
        secret = os.environ.get("CAPTURE_SECRET", "").encode("utf-8")
        return data.replace(secret, b"[REDACTED]") if secret else data

    def save_raw(self, stdout: bytes, stderr: bytes, exit_code: int | None,
                 timed_out: bool):
        self.save_calls.append((stdout, stderr, exit_code, timed_out))
        if self.save_mode == "none":
            return None
        if self.save_mode == "throw":
            raise OSError("archive unavailable")
        directory = self.artifact_root / "output-artifacts"
        path = directory
        if self.path_length:
            while len(str(path)) < self.path_length:
                path /= "d" * 40
        path.mkdir(parents=True, exist_ok=True)
        file_name = "shell-raw-" + "x" * 80 + ".json"
        archive = path / file_name
        payload = json.dumps({
            "version": 2,
            "exit_code": exit_code,
            "timed_out": bool(timed_out),
            "stdout_text": stdout.decode("utf-8", "replace"),
            "stderr_text": stderr.decode("utf-8", "replace"),
        }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        archive.write_bytes(payload)
        return {"id": "fixture-artifact", "path": str(archive), "bytes": len(payload)}


class RTKCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.command = self.bin_dir / "pytest"
        self.command.write_text(FAKE_COMMAND, encoding="utf-8")
        self.command.chmod(0o755)
        self.count_file = self.root / "command-count"
        self.env_patch = patch.dict(os.environ, {
            "PATH": str(self.bin_dir) + os.pathsep + os.environ.get("PATH", ""),
            "CAPTURE_COUNT_FILE": str(self.count_file),
            "CAPTURE_MODE": "large",
        }, clear=False)
        self.env_patch.start()

    def tearDown(self):
        self.env_patch.stop()
        self.temp.cleanup()

    def _run(self, mode: str, filtered: FilterResult | None, *, save_mode: str = "ok",
             cap: int = 1000, timeout: float = 30, path_length: int = 0,
             filter_error: BaseException | None = None):
        compressor = CaptureCompressor(self.root / "artifacts", save_mode, path_length)
        with patch.dict(os.environ, {"CAPTURE_MODE": mode}, clear=False):
            if filter_error is None:
                context = patch("termux_agent.tools.run_rtk_filter", return_value=filtered)
            else:
                context = patch("termux_agent.tools.run_rtk_filter", side_effect=filter_error)
            with context:
                result = execute("shell", {"command": "pytest", "timeout": timeout}, cap,
                                 output_compressor=compressor)
        return result, compressor

    def _assert_once(self):
        self.assertEqual(self.count_file.read_text(encoding="utf-8").splitlines(), ["run"])

    def test_over_cap_stderr_candidate_keeps_actual_stream_and_original_runs_once(self):
        result, compressor = self._run(
            "large", FilterResult(b"pytest: 1 failed", 0), cap=3000)
        self._assert_once()
        self.assertEqual(result["exit_code"], 7)
        self.assertEqual(result["stdout"], "pytest: 1 failed")
        self.assertIn("CRITICAL_STDERR_SENTINEL", result["stderr"])
        metadata = result["output_compression"]
        self.assertTrue(metadata["applied"])
        self.assertTrue(metadata["raw_available"])
        self.assertGreater(metadata["before_bytes"], metadata["after_bytes"])
        self.assertEqual(metadata["after_bytes"], len(serialized_result_bytes(result)))
        self.assertGreater(compressor.last_stats["raw_source_bytes"], 50000)

    def test_archive_none_and_throw_fall_back_without_replacing_output(self):
        for save_mode in ("none", "throw"):
            with self.subTest(save_mode=save_mode):
                self.count_file.unlink(missing_ok=True)
                result, compressor = self._run(
                    "large", FilterResult(b"pytest: 1 failed", 0), save_mode=save_mode)
                self._assert_once()
                self.assertEqual(result["exit_code"], 7)
                self.assertIn("CRITICAL_STDERR_SENTINEL", result["stderr"])
                self.assertIn("[TRUNCATED]", result["stdout"])
                self.assertFalse(result["output_compression"]["applied"])
                self.assertFalse(result["output_compression"]["raw_available"])
                self.assertEqual(result["output_compression"]["status"], "archive_failed")
                self.assertEqual(compressor.last_stats["before_bytes"], 0)
                self.assertEqual(compressor.last_stats["after_bytes"], 0)

    def test_original_is_never_replayed_for_empty_invalid_error_or_timeout_filters(self):
        cases = (
            (FilterResult(b"", 0), None),
            (FilterResult(b"\xff", 0), None),
            (FilterResult(None, 1, stderr=b"filter diagnostic"), None),
            (FilterResult(None, None, timed_out=True), None),
            (None, RuntimeError("filter exploded")),
        )
        for filtered, error in cases:
            with self.subTest(filtered=filtered, error=error):
                self.count_file.unlink(missing_ok=True)
                result, compressor = self._run(
                    "medium", filtered, filter_error=error, cap=2000)
                self._assert_once()
                self.assertIn("AAAA", result["stdout"])
                self.assertFalse(result["output_compression"]["applied"])
                self.assertFalse(compressor.last_stats["applied"])

    def test_exact_legacy_fallback_and_false_savings_are_zero(self):
        with patch.dict(os.environ, {"CAPTURE_MODE": "identity"}, clear=False):
            plain = execute("shell", {"command": "pytest"}, 20000)
        self.count_file.unlink(missing_ok=True)
        result, compressor = self._run(
            "identity", FilterResult(b"B" * 5000, 0), cap=20000)
        self.assertEqual(result, plain)
        self.assertNotIn("output_compression", result)
        self.assertEqual(compressor.last_stats["status"], "no_benefit")
        self.assertEqual(compressor.last_stats["before_bytes"], 0)
        self.assertEqual(compressor.last_stats["after_bytes"], 0)
        self.assertGreater(compressor.last_stats["raw_source_bytes"], 0)

    def test_actual_artifact_metadata_growth_rejects_candidate(self):
        result, compressor = self._run(
            "medium", FilterResult(b"tiny", 0), cap=2000, path_length=500)
        self.assertNotIn("output_compression", result)
        self.assertEqual(result["stdout"], "A" * 800)
        self.assertEqual(result["stderr"], "diagnostic")
        self.assertEqual(compressor.last_stats["status"], "no_benefit")
        self.assertEqual(compressor.last_stats["before_bytes"], 0)
        self.assertEqual(compressor.last_stats["after_bytes"], 0)
        self.assertEqual(len(compressor.save_calls), 1)

    def test_small_final_saving_does_not_pay_capture_metadata_cost(self):
        result, compressor = self._run(
            "medium", FilterResult(b"A" * 300, 0), cap=3000)
        self._assert_once()
        self.assertNotIn("output_compression", result)
        self.assertEqual(result["stdout"], "A" * 800)
        self.assertEqual(result["stderr"], "diagnostic")
        self.assertEqual(compressor.last_stats["status"], "no_benefit")
        self.assertFalse(compressor.last_stats["applied"])
        self.assertEqual(len(compressor.save_calls), 1)

    def test_empty_filter_uses_capture_and_does_not_replay(self):
        result, compressor = self._run(
            "medium", FilterResult(b"", 0), cap=2000)
        self._assert_once()
        self.assertIn("AAAA", result["stdout"])
        self.assertEqual(result["output_compression"]["status"], "filter_failed")
        self.assertTrue(result["output_compression"]["raw_available"])
        self.assertFalse(compressor.last_stats["applied"])

    def test_raw_capture_is_full_before_legacy_clipping(self):
        result, compressor = self._run(
            "large", FilterResult(b"short", 0), cap=3000)
        self._assert_once()
        payload = json.loads(Path(result["output_compression"]["raw_artifact"]["path"]).read_text(encoding="utf-8"))
        self.assertEqual(len(payload["stdout_text"]), 50000)
        self.assertEqual(payload["stderr_text"], "CRITICAL_STDERR_SENTINEL")
        self.assertEqual(payload["exit_code"], 7)
        self.assertGreater(compressor.last_stats["raw_source_bytes"], 50000)

    def test_filter_output_and_error_details_are_redacted(self):
        secret = "capture-secret-value"
        with patch.dict(os.environ, {"CAPTURE_SECRET": secret}, clear=False):
            result, _ = self._run(
                "secret", FilterResult(("summary " + secret).encode("utf-8"), 0,
                                       stderr=secret.encode("utf-8")), cap=3000)
        self.assertNotIn(secret, json.dumps(result, ensure_ascii=False))
        self.assertIn("[REDACTED]", json.dumps(result, ensure_ascii=False))

    def test_timeout_notice_is_included_inside_output_cap(self):
        result, _ = self._run(
            "sleep", FilterResult(b"short", 0), cap=2000, timeout=0.1)
        combined = result["stdout"] + ("\n" if result["stdout"] and result["stderr"] else "") + result["stderr"]
        self.assertLessEqual(len(combined), 2000)
        self.assertIn("command timed out", json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
