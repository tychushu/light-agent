"""Optional browser tool: gates, strict arguments, bounded output and cleanup."""
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from termux_agent import approval, tools
from termux_agent.agent import Agent
from termux_agent.config import Config


class NullClient:
    def __init__(self, *args, **kwargs):
        pass

    def close(self):
        pass


def fake_binary(directory, body):
    path = Path(directory) / "lightpanda-fake"
    path.write_text("#!" + shutil.which("bash") + "\n" + body + "\n")
    path.chmod(0o755)
    return str(path)


def payload(body):
    return shlex.quote(sys.executable) + " -c 'import json,sys;print(json.dumps(" + body + "))'"


class WebFetchToolTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = self._td.name
        self.addCleanup(self._td.cleanup)

    def fetch(self, args, cap=2000, env_binary=None, binary=None, timeout=None):
        if env_binary is not None:
            env = patch.dict(os.environ, {"TERMUX_AGENT_LIGHTPANDA": env_binary})
            env.start()
            self.addCleanup(env.stop)
        return tools._web_fetch(args, cap, binary=binary, timeout=timeout)

    def test_schema_is_registered_and_strict(self):
        schema = tools.WEB_FETCH_TOOL["function"]
        self.assertEqual(schema["name"], "web_fetch")
        self.assertIn("url", schema["parameters"]["properties"])
        self.assertEqual(schema["parameters"]["required"], ["url"])
        self.assertIs(schema["parameters"]["additionalProperties"], False)
        self.assertEqual(schema["parameters"]["properties"]["format"]["enum"], ["markdown", "html", "tree"])
        self.assertEqual(schema["parameters"]["properties"]["strip"]["enum"], ["none", "clutter", "shell"])
        self.assertNotIn(tools.WEB_FETCH_TOOL, tools.TOOLS)

    def test_dispatch_parses_the_json_envelope(self):
        exe = fake_binary(self.tmp, payload('{"url":"https://x.test/","http_status":201,"content":"# ok","error":None}'))
        with patch.dict(os.environ, {"TERMUX_AGENT_LIGHTPANDA": exe}):
            result = tools.execute("web_fetch", {"url": "https://x.test/"})
        self.assertNotIn("error", result)
        self.assertEqual(result["status"], 201)
        self.assertEqual(result["content"], "# ok")
        self.assertEqual(result["chars"], 4)
        self.assertFalse(result["truncated"])
        self.assertEqual(result["format"], "markdown")

    def test_text_dumps_pass_the_byte_cap_but_tree_dumps_do_not(self):
        body = 'printf "%s\\n" "$@" > "$0.argv"\n' + payload('{"url":"https://x.test/","http_status":200,"content":"a"*4000}')
        exe = fake_binary(self.tmp, body)
        self.fetch({"url": "https://x.test/", "format": "html"}, cap=500, binary=exe)
        argv = (Path(exe + ".argv")).read_text().splitlines()
        self.assertIn("--dump-max-bytes", argv)
        self.assertEqual(argv[argv.index("--dump-max-bytes") + 1], "2004")
        self.assertEqual(argv[argv.index("--dump") + 1], "html")
        tree = self.fetch({"url": "https://x.test/", "format": "tree"}, cap=500, binary=exe)
        argv = (Path(exe + ".argv")).read_text().splitlines()
        self.assertNotIn("--dump-max-bytes", argv, "semantic_tree_text rejects the flag")
        self.assertEqual(argv[argv.index("--dump") + 1], "semantic_tree_text")
        self.assertTrue(tree["truncated"])
        self.assertLessEqual(len(tree["content"]), 500)
        self.assertIn("[TRUNCATED]", tree["content"])

    def test_strip_and_selector_become_flags(self):
        exe = fake_binary(self.tmp, 'printf "%s\\n" "$@" > "$0.argv"\n' + payload('{"url":"https://x.test/","http_status":200,"content":"x"}'))
        self.fetch({"url": "https://x.test/", "strip": "clutter", "selector": "main", "wait_ms": 1500}, binary=exe)
        argv = (Path(exe + ".argv")).read_text().splitlines()
        self.assertEqual(argv[argv.index("--strip-mode") + 1], "clutter")
        self.assertEqual(argv[argv.index("--dump-selector") + 1], "main")
        self.assertEqual(argv[argv.index("--wait-ms") + 1], "1500")
        self.assertNotIn("--strip-mode", argv[:argv.index("--strip-mode")], "strip mode must be opt-in")

    def test_plain_text_output_falls_back_without_status(self):
        exe = fake_binary(self.tmp, "echo raw body")
        result = self.fetch({"url": "https://x.test/"}, binary=exe)
        self.assertEqual(result["content"].strip(), "raw body")
        self.assertIsNone(result["status"])

    def test_nonzero_exit_surfaces_the_browser_reason(self):
        exe = fake_binary(self.tmp, payload('{"url":"https://x.test/","http_status":None,"content":"","error":"navigate_timeout"}') + "\nexit 1")
        with self.assertRaises(ValueError) as caught:
            self.fetch({"url": "https://x.test/"}, binary=exe)
        self.assertIn("navigate_timeout", str(caught.exception))

    def test_timeout_terminates_promptly(self):
        exe = fake_binary(self.tmp, "sleep 30")
        with patch("os.killpg", wraps=os.killpg) as killed:
            with self.assertRaises(ValueError) as caught:
                self.fetch({"url": "https://x.test/"}, binary=exe, timeout=0.5)
        self.assertIn("timed out", str(caught.exception))
        killed.assert_called()
        self.assertIn(signal.SIGTERM, [args.args[1] for args in killed.call_args_list])
        self.assertIn(signal.SIGKILL, [args.args[1] for args in killed.call_args_list])

    def test_invalid_arguments_are_tool_errors(self):
        cases = [
            {"url": "file:///etc/passwd"},
            {"url": "javascript:alert(1)"},
            {"url": "   "},
            {"url": "https://x.test/", "format": "pdf"},
            {"url": "https://x.test/", "strip": "bogus"},
            {"url": "https://x.test/", "wait_ms": -1},
            {"url": "https://x.test/", "wait_ms": 900000},
            {"url": "https://x.test/", "wait_ms": True},
            {"url": "https://x.test/", "selector": "  "},
            {"url": "https://"},
            {"url": "https://x.test:invalid/"},
            {"url": "https://x.test/", "format": []},
            {"url": "https://x.test/", "strip": {}},
            {"url": "https://x.test/", "extra": True},
            {},
        ]
        for args in cases:
            with patch("termux_agent.tools.subprocess.Popen") as spawn:
                result = tools.execute("web_fetch", args)
            spawn.assert_not_called()
            self.assertIn("error", result, args)
            self.assertIn("ValueError", result["error"], args)

    def test_read_only_fetch_needs_no_approval(self):
        args = {"url": "https://example.com"}
        self.assertEqual(approval.reason("web_fetch", args), "")
        self.assertFalse(approval.needs_approval("web_fetch", args, "on-risk"))
        self.assertTrue(approval.needs_approval("web_fetch", args, "always"))

    def test_missing_binary_hides_the_tool(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"HOME": td}):
            with patch("termux_agent.agent.web_fetch_available", return_value=False):
                hidden = Agent(Config(web_fetch_enabled=True), client=NullClient())
            with patch("termux_agent.agent.web_fetch_available", return_value=True):
                shown = Agent(Config(web_fetch_enabled=True), client=NullClient())
            with patch("termux_agent.agent.web_fetch_available") as detection:
                default = Agent(Config(), client=NullClient())
            detection.assert_not_called()
        names = lambda agent: [t["function"]["name"] for t in agent.tools]
        self.assertNotIn("web_fetch", names(hidden))
        self.assertNotIn("web_fetch", names(default))
        self.assertIn("web_fetch", names(shown))
        self.assertIn("shell", names(shown))

    def test_unresolvable_binary_is_a_clear_error(self):
        self.assertFalse(tools.web_fetch_available("/nonexistent/lightpanda"))
        self.assertFalse(tools.web_fetch_available("certainly-not-a-binary"))
        with patch.dict(os.environ, {"TERMUX_AGENT_LIGHTPANDA": "/nonexistent/lightpanda"}):
            result = tools.execute("web_fetch", {"url": "https://example.com"})
        self.assertIn("lightpanda binary not found", result["error"])

    def test_config_defaults_to_off_and_requires_valid_types(self):
        self.assertFalse(Config().web_fetch_enabled)
        for field, values in {"web_fetch_enabled": (1, "true", None),
                              "lightpanda_binary": (True, None, "bad\npath")}.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    Config(**{field: value}).validate()
        path = Path(self.tmp) / 'config.toml'
        path.write_text('web_fetch_enabled=true\nlightpanda_binary="~/bin/lightpanda"\n')
        loaded = Config.load(path)
        self.assertTrue(loaded.web_fetch_enabled)
        self.assertEqual(loaded.lightpanda_binary, '~/bin/lightpanda')

    def test_explicit_binary_overrides_environment_and_directories_are_not_executables(self):
        env_exe = fake_binary(self.tmp, payload('{"content":"environment"}'))
        folder = Path(self.tmp) / 'explicit'
        folder.mkdir()
        explicit = fake_binary(folder, payload('{"content":"explicit"}'))
        with patch.dict(os.environ, {'TERMUX_AGENT_LIGHTPANDA': env_exe}):
            result = tools.execute('web_fetch', {'url': 'https://x.test'}, lightpanda_binary=explicit)
        self.assertEqual(result['content'], 'explicit')
        self.assertFalse(tools.web_fetch_available(self.tmp))

    def test_telemetry_is_disabled_log_level_is_error_and_selector_is_literal(self):
        marker = Path(self.tmp) / 'injected'
        selector = 'main[data-label="two words"]; touch ' + str(marker)
        body = ('printf "%s\\n" "$@" > "$0.argv"\n'
                'printf "%s" "$LIGHTPANDA_DISABLE_TELEMETRY" > "$0.telemetry"\n' + payload('{"content":"ok"}'))
        exe = fake_binary(self.tmp, body)
        with patch.dict(os.environ, {'LIGHTPANDA_DISABLE_TELEMETRY': 'false'}):
            self.fetch({'url': 'https://x.test', 'selector': selector}, binary=exe)
        argv = Path(exe + '.argv').read_text().splitlines()
        self.assertEqual(argv[argv.index('--dump-selector') + 1], selector)
        self.assertEqual(argv[argv.index('--log-level') + 1], 'error')
        self.assertEqual(Path(exe + '.telemetry').read_text(), 'true')
        self.assertFalse(marker.exists())

    def test_http_errors_keep_the_body_and_use_http_status_and_final_url(self):
        for status in (404, 503):
            with self.subTest(status=status):
                exe = fake_binary(self.tmp, payload('{"results":[{"url":"https://final.test/","http_status":' +
                                  str(status) + ',"status":200,"content":"error page body"}]}'))
                result = self.fetch({'url': 'https://x.test'}, binary=exe)
                self.assertEqual(result['status'], status)
                self.assertEqual(result['url'], 'https://final.test/')
                self.assertEqual(result['content'], 'error page body')

    def test_utf8_browser_byte_clipping_still_reports_character_truncation(self):
        code = ('import json,sys; n=int(sys.argv[sys.argv.index("--dump-max-bytes")+1]); '
                's=("😀"*1000).encode()[:n].decode("utf-8","ignore"); '
                'print(json.dumps({"content":s,"http_status":200}))')
        exe = fake_binary(self.tmp, shlex.quote(sys.executable) + ' -c ' + shlex.quote(code) + ' "$@"')
        result = self.fetch({'url': 'https://x.test'}, cap=500, binary=exe)
        self.assertTrue(result['truncated'])
        self.assertEqual(result['chars'], len(result['content']))
        self.assertLessEqual(result['chars'], 500)

    def test_oversized_stdout_fails_bounded_and_stderr_is_drained_and_bounded(self):
        exe = fake_binary(self.tmp, payload('{"content":"x"*200000}'))
        with self.assertRaisesRegex(ValueError, 'byte limit'):
            self.fetch({'url': 'https://x.test', 'format': 'tree'}, cap=128, binary=exe)
        code = 'import sys; sys.stderr.write("x"*200000+"DETAIL_TAIL"); sys.exit(1)'
        exe = fake_binary(self.tmp, shlex.quote(sys.executable) + ' -c ' + shlex.quote(code))
        with self.assertRaises(ValueError) as error:
            self.fetch({'url': 'https://x.test'}, binary=exe)
        self.assertIn('DETAIL_TAIL', str(error.exception))
        self.assertLess(len(str(error.exception)), 500)

    def test_timeout_kills_descendant_even_if_leader_exits_after_term(self):
        exe = fake_binary(self.tmp, '(trap "" TERM; sleep 0.6; echo orphan > "$0.leak") >/dev/null 2>&1 &\nsleep 30')
        with self.assertRaisesRegex(ValueError, 'timed out'):
            self.fetch({'url': 'https://x.test'}, binary=exe, timeout=0.1)
        time.sleep(0.8)
        self.assertFalse(Path(exe + '.leak').exists())

    def test_keyboard_interrupt_reaps_the_browser(self):
        exe = fake_binary(self.tmp, 'sleep 30')
        selector = tools.selectors.DefaultSelector()
        real_popen = subprocess.Popen
        processes = []
        def spawn(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            processes.append(proc)
            return proc
        with patch('termux_agent.tools.selectors.DefaultSelector', return_value=selector), \
                patch.object(selector, 'select', side_effect=KeyboardInterrupt), \
                patch('termux_agent.tools.subprocess.Popen', side_effect=spawn):
            with self.assertRaises(KeyboardInterrupt):
                self.fetch({'url': 'https://x.test'}, binary=exe)
        self.assertIsNotNone(processes[0].poll())

    def test_disabled_or_missing_tool_cannot_be_invoked_by_model(self):
        class Model(NullClient):
            def chat(self, messages, schemas):
                if len(messages) == 2:
                    return {"content": "", "tool_calls": [{"id": "fetch_a", "type": "function", "function": {
                        "name": "web_fetch", "arguments": '{"url":"https://x.test"}'}}]}
                return {"content": "done"}
        for enabled, available in ((False, True), (True, False)):
            with self.subTest(enabled=enabled), patch('termux_agent.agent.web_fetch_available', return_value=available):
                agent = Agent(Config(web_fetch_enabled=enabled, approval_policy='never'), client=Model(), base_system='test')
                with patch('termux_agent.agent.execute') as execute:
                    agent.run('fetch')
                execute.assert_not_called()
                self.assertIn('disabled or', agent.messages[-2]['content'])

    def test_enabled_subagent_inherits_browser_path_and_optional_tool(self):
        class Worker(NullClient):
            schemas = None
            def chat(self, messages, schemas):
                self.schemas = schemas
                if len(messages) == 2:
                    return {"content": "", "tool_calls": [{"id": "fetch_a", "type": "function", "function": {
                        "name": "web_fetch", "arguments": '{"url":"https://x.test"}'}}]}
                return {"content": "done"}
        worker = Worker()
        with patch.dict(os.environ, {'HOME': self.tmp}), patch('termux_agent.agent.web_fetch_available', return_value=True):
            agent = Agent(Config(web_fetch_enabled=True, lightpanda_binary='/explicit/browser', approval_policy='never'),
                          client=NullClient(), base_system='test')
            with patch('termux_agent.agent.Client', return_value=worker), \
                    patch('termux_agent.tools._web_fetch', return_value={'content': 'ok'}) as fetch:
                self.assertEqual(agent.delegate('fetch')['status'], 'complete')
        fetch.assert_called_once_with({'url': 'https://x.test'}, 20000, binary='/explicit/browser')
        names = [schema['function']['name'] for schema in worker.schemas]
        self.assertIn('web_fetch', names)
        self.assertNotIn('delegate', names)
