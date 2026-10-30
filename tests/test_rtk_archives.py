import base64
import hashlib
import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from termux_agent.output_compression import (
    ARTIFACT_RETENTION,
    FILTER_OUTPUT_BYTES,
    OutputCompressor,
    classify_shell_command,
    redact_bytes,
    resolve_rtk_binary,
    run_rtk_filter,
    save_raw_artifact,
    secret_values,
    stable_result_payload,
)
from termux_agent.tools import execute


class ClassifierAndBinaryTests(unittest.TestCase):
    def test_classifier_rejects_substitutions_compounds_and_machine_clusters(self):
        accepted = {
            "git status": "git-status",
            "git log -n 10": "git-log",
            "rg -n pattern": "rg",
            "grep -n pattern file": "grep",
            "python3 -m pytest tests": "pytest",
        }
        for command, expected in accepted.items():
            with self.subTest(command=command):
                self.assertEqual(classify_shell_command(command), expected)

        rejected = (
            "pytest $(echo run)",
            "pytest `echo run`",
            "git status && echo done",
            "git status | cat",
            "git status > result.txt",
            "git status --porcelain=v1",
            "git status -sb",
            "git status -b",
            "git log --format=%h",
            "git log -pn",
            "rg -nJ pattern",
            "grep -no pattern file",
            "pytest --junitxml=results.xml",
            "python -m pytest --json-report=report.json",
        )
        for command in rejected:
            with self.subTest(command=command):
                self.assertIsNone(classify_shell_command(command))

    def test_explicit_binary_is_authoritative_and_empty_config_uses_env_or_path(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as temporary:
            root = Path(temporary)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            env_rtk = bin_dir / "env-rtk"
            path_rtk = bin_dir / "rtk"
            for executable in (env_rtk, path_rtk):
                executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                executable.chmod(0o755)
            with patch.dict(os.environ, {"TERMUX_AGENT_RTK": str(env_rtk),
                                         "PATH": str(bin_dir)}, clear=False):
                self.assertEqual(resolve_rtk_binary(str(root / "missing")), "")
                self.assertEqual(resolve_rtk_binary(""), str(env_rtk))
            with patch.dict(os.environ, {"TERMUX_AGENT_RTK": "",
                                         "PATH": str(bin_dir)}, clear=False):
                self.assertEqual(resolve_rtk_binary(""), str(path_rtk))

    def test_last_stats_is_reset_for_every_plan_attempt(self):
        compressor = OutputCompressor(SimpleNamespace(rtk_enabled=False, rtk_binary="",
                                                       api_key=""))
        self.assertIsNone(compressor.last_stats)
        compressor.last_stats = {"status": "stale"}
        self.assertIsNone(compressor.plan("git status"))
        self.assertIsNone(compressor.last_stats)


class FilterProcessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def fixture(self, body: str) -> Path:
        path = self.root / "filter-fixture"
        path.write_text("#!" + sys.executable + "\n" + body, encoding="utf-8")
        path.chmod(0o755)
        return path

    def test_filter_uses_fixed_argv_waits_for_leader_and_preserves_exit_code(self):
        fixture = self.fixture(
            "import json,sys,time\n"
            "sys.stdout.write(json.dumps(sys.argv[1:]))\n"
            "sys.stdout.flush()\n"
            "sys.stderr.write('filter-stderr')\n"
            "sys.stderr.flush()\n"
            "sys.stdout.close()\n"
            "sys.stderr.close()\n"
            "time.sleep(0.12)\n"
            "raise SystemExit(23)\n"
        )
        result = run_rtk_filter(str(fixture), "pytest", b"payload\n", timeout=1.0)
        self.assertEqual(result.returncode, 23)
        self.assertEqual(json.loads(result.output.decode("utf-8")),
                         ["pipe", "--filter", "pytest"])
        self.assertEqual(result.stderr, b"filter-stderr")
        self.assertFalse(result.timed_out)

    def test_filter_output_is_bounded(self):
        fixture = self.fixture(
            "import os\n"
            f"os.write(1, b'x' * ({FILTER_OUTPUT_BYTES} + 128))\n"
        )
        result = run_rtk_filter(str(fixture), "pytest", b"payload\n", timeout=2.0)
        self.assertTrue(result.output_overflow)
        self.assertLessEqual(len(result.output or b""), FILTER_OUTPUT_BYTES)

    def test_filter_result_output_and_error_details_are_redacted(self):
        secret = "filter-detail-secret"
        fixture = self.fixture(
            "import os,sys\n"
            f"sys.stdout.write({secret!r})\n"
            f"sys.stderr.write({secret!r})\n"
        )
        with patch.dict(os.environ, {"TERMUX_AGENT_SECRET": secret}, clear=False):
            result = run_rtk_filter(str(fixture), "pytest", b"payload\n", timeout=1.0)
        self.assertNotIn(secret.encode("utf-8"), result.output)
        self.assertNotIn(secret.encode("utf-8"), result.stderr)

        with patch("termux_agent.output_compression._bounded_process",
                   return_value=type(result)(None, None, error="detail " + secret)), \
                patch.dict(os.environ, {"TERMUX_AGENT_SECRET": secret}, clear=False):
            result = run_rtk_filter(str(fixture), "pytest", b"payload\n", timeout=1.0)
        self.assertNotIn(secret, result.error)

    def test_interrupt_kills_process_group_and_reaps_child(self):
        marker = self.root / "late-child"
        child_command = "import time; time.sleep(.4); open(%r, 'w').write('late')" % str(marker)
        fixture = self.fixture(
            "import subprocess,sys,time\n"
            "subprocess.Popen([sys.executable, '-c', %r])\n" % child_command +
            "sys.stdout.write('ready')\n"
            "sys.stdout.flush()\n"
            "time.sleep(2)\n"
        )

        def interrupt(*args, **kwargs):
            time.sleep(0.1)
            raise KeyboardInterrupt

        with patch("termux_agent.output_compression.selectors.DefaultSelector.select",
                   side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                run_rtk_filter(str(fixture), "pytest", b"payload\n", timeout=2.0)
        time.sleep(0.55)
        self.assertFalse(marker.exists())


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.root = Path(self.temporary.name)
        self.store = self.root / "store"
        self.store.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def read_payload(self, artifact):
        return json.loads(Path(artifact["path"]).read_text(encoding="utf-8"))

    def test_existing_shared_and_container_directory_modes_are_preserved(self):
        home = self.root / "home"
        local = home / ".local"
        share = local / "share"
        share.mkdir(parents=True)
        local.chmod(0o751)
        share.chmod(0o750)

        with patch.dict(os.environ, {"HOME": str(home)}, clear=False):
            home_artifact = save_raw_artifact(b"home", b"", 0, False)
        self.assertIsNotNone(home_artifact)
        self.assertEqual(stat.S_IMODE(local.stat().st_mode), 0o751)
        self.assertEqual(stat.S_IMODE(share.stat().st_mode), 0o750)
        self.assertEqual(
            stat.S_IMODE((share / "termux-agent").stat().st_mode), 0o700)

        self.store.chmod(0o710)
        root_artifact = save_raw_artifact(b"root", b"", 0, False, root=self.store)
        self.assertIsNotNone(root_artifact)
        self.assertEqual(stat.S_IMODE(self.store.stat().st_mode), 0o710)

        for artifact in (home_artifact, root_artifact):
            artifact_path = Path(artifact["path"])
            data_dir = artifact_path.parent
            self.assertEqual(stat.S_IMODE(data_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(artifact_path.stat().st_mode), 0o600)

    def test_utf8_streams_are_directly_readable_and_binary_uses_base64_only(self):
        stdout = "first line\nsecond é中".encode("utf-8")
        stderr = "error\n".encode("utf-8")
        artifact = save_raw_artifact(stdout, stderr, 7, False, root=self.store)
        self.assertIsNotNone(artifact)
        payload = self.read_payload(artifact)
        self.assertEqual(payload["stdout_text"], stdout.decode("utf-8"))
        self.assertEqual(payload["stderr_text"], stderr.decode("utf-8"))
        self.assertNotIn("stdout_base64", payload)
        self.assertNotIn("stderr_base64", payload)
        self.assertEqual(payload["exit_code"], 7)
        self.assertEqual(payload["stdout_sha256"], hashlib.sha256(stdout).hexdigest())
        visible = execute("read_file", {"path": artifact["path"], "offset": 0,
                                        "limit": 100000}, max_output_chars=100000)
        self.assertEqual(json.loads(visible["content"])["stdout_text"], stdout.decode("utf-8"))

        binary = b"\x00\xff\x80"
        artifact = save_raw_artifact(binary, b"", 0, True, root=self.store)
        payload = self.read_payload(artifact)
        self.assertEqual(base64.b64decode(payload["stdout_base64"]), binary)
        self.assertNotIn("stdout_text", payload)

    def test_short_writes_are_completed_and_read_back_before_publish(self):
        real_write = os.write

        def short_write(fd, data):
            return real_write(fd, data[:1])

        stdout = b"complete-short-write-payload" * 20
        with patch("termux_agent.output_compression.os.write", side_effect=short_write):
            artifact = save_raw_artifact(stdout, b"tail", 2, False, root=self.store)
        self.assertIsNotNone(artifact)
        payload = self.read_payload(artifact)
        self.assertEqual(payload["stdout_text"].encode("utf-8"), stdout)

    def test_publication_works_without_os_link(self):
        had_link = hasattr(os, "link")
        real_link = getattr(os, "link", None)
        if had_link:
            del os.link
        try:
            self.assertFalse(hasattr(os, "link"))
            artifact = save_raw_artifact(b"portable-publication", b"", 0, False,
                                         root=self.store)
        finally:
            if had_link:
                os.link = real_link

        self.assertIsNotNone(artifact)
        self.assertEqual(self.read_payload(artifact)["stdout_text"],
                         "portable-publication")
        self.assertEqual(
            list((self.store / "output-artifacts").glob(".shell-raw-*.tmp")), [])

    def test_parallel_writers_obey_retention_quota_and_clean_owned_temporaries(self):
        directory = self.store / "output-artifacts"
        directory.mkdir()
        unrelated = directory / "unrelated.tmp"
        unrelated.write_text("keep", encoding="utf-8")
        stale = directory / ".shell-raw-0011223344556677.8899aabb.tmp"
        stale.write_bytes(b"stale")

        with patch("termux_agent.output_compression.ARTIFACT_RETENTION", 3):
            with ThreadPoolExecutor(max_workers=8) as executor:
                rows = list(executor.map(
                    lambda index: save_raw_artifact(("payload-%02d" % index).encode("ascii"),
                                                     b"stderr", index, False, root=self.store),
                    range(10)))
        self.assertTrue(all(rows))
        generated = [item for item in directory.iterdir()
                     if item.name.startswith("shell-raw-") and item.suffix == ".json"]
        self.assertLessEqual(len(generated), 3)
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep")
        self.assertFalse(stale.exists())
        self.assertEqual(list(directory.glob(".shell-raw-*.tmp")), [])

    def test_symlink_children_fail_without_modifying_outside_store(self):
        outside = self.root / "outside"
        outside.mkdir()
        sentinel = outside / "sentinel"
        sentinel.write_text("keep", encoding="utf-8")
        (self.store / "output-artifacts").symlink_to(outside, target_is_directory=True)
        self.assertIsNone(save_raw_artifact(b"payload", b"", 0, False, root=self.store))
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
        self.assertEqual(list(outside.iterdir()), [sentinel])

    def test_trusted_home_alias_works_but_symlink_below_home_is_refused(self):
        real_home = self.root / "real-home"
        real_home.mkdir()
        alias = self.root / "home-alias"
        alias.symlink_to(real_home, target_is_directory=True)
        with patch.dict(os.environ, {"HOME": str(alias)}, clear=False):
            artifact = save_raw_artifact(b"alias", b"", 0, False)
        self.assertIsNotNone(artifact)
        self.assertTrue(Path(artifact["path"]).is_file())
        self.assertTrue(str(Path(artifact["path"])).startswith(str(real_home)))

        unsafe_home = self.root / "unsafe-home"
        unsafe_home.mkdir()
        outside = self.root / "outside-home"
        outside.mkdir()
        (unsafe_home / ".local").symlink_to(outside, target_is_directory=True)
        with patch.dict(os.environ, {"HOME": str(unsafe_home)}, clear=False):
            self.assertIsNone(save_raw_artifact(b"unsafe", b"", 0, False))
        self.assertEqual(list(outside.iterdir()), [])

        parent = unsafe_home / "parent-link"
        parent.symlink_to(outside, target_is_directory=True)
        with patch.dict(os.environ, {"HOME": str(unsafe_home)}, clear=False):
            self.assertIsNone(save_raw_artifact(b"unsafe-root", b"", 0, False,
                                                root=parent / "store"))
        self.assertEqual(list(outside.iterdir()), [])

    def test_env_and_configured_secrets_are_redacted(self):
        configured = "configured-secret-value"
        env_secret = "environment-secret-value"
        data = (configured + "/" + env_secret).encode("utf-8")
        with patch.dict(os.environ, {"TERMUX_AGENT_API_TOKEN": env_secret}, clear=False):
            self.assertIn(env_secret, secret_values((configured,)))
            redacted = redact_bytes(data, (configured,))
        self.assertNotIn(configured.encode("utf-8"), redacted)
        self.assertNotIn(env_secret.encode("utf-8"), redacted)


class StablePayloadTests(unittest.TestCase):
    def test_stable_payload_changes_only_compression_metadata(self):
        outside = {
            "path": "/ordinary/path",
            "duration": 0.1,
            "timing": {"started_at": 1, "elapsed": 2},
            "user_data": {
                "path": "/user/path",
                "duration": 3,
                "filter_reason": "keep-outside",
                "nested": [{"refs": ["keep"], "created_at": "now"}],
            },
        }
        first = {
            **outside,
            "stdout": "same output",
            "stderr": "same error",
            "exit_code": 4,
            "timed_out": False,
            "output_compression": {
                "status": "compressed",
                "applied": True,
                "raw_digest": "abc123",
                "raw_artifact": {"id": "one", "path": "/one", "bytes": 10},
                "filter_reason": "temporary",
                "filter_error": "detail-one",
                "before_bytes": 100,
                "after_bytes": 20,
                "raw_source_bytes": 100,
                "raw_source_chars": 90,
            },
        }
        second = {
            **first,
            "output_compression": {
                **first["output_compression"],
                "raw_artifact": {"id": "two", "path": "/two", "bytes": 99},
                "filter_reason": "other",
                "filter_error": "detail-two",
                "before_bytes": 120,
                "after_bytes": 30,
                "raw_source_bytes": 120,
                "raw_source_chars": 110,
            },
        }
        stable_one = stable_result_payload(first)
        stable_two = stable_result_payload(second)
        expected = {
            **outside,
            "stdout": "same output",
            "stderr": "same error",
            "exit_code": 4,
            "timed_out": False,
            "output_compression": {
                "status": "compressed",
                "applied": True,
                "raw_digest": "abc123",
            },
        }
        self.assertEqual(stable_one, expected)
        self.assertEqual(stable_one, stable_two)


if __name__ == "__main__":
    unittest.main()
