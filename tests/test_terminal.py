"""Exercise real terminal input bytes, not mocked Python input()."""
import json
import fcntl
import io
import os
import pty
import re
import select
import signal
import sys
import struct
import subprocess
import tempfile
import termios
import time
import unittest
from datetime import datetime


class TerminalTests(unittest.TestCase):
    @staticmethod
    def unwrap_timestamped(text):
        prefix = re.compile(r"^(?:\d{2}:\d{2} [^:│]+: |\d{2}:\d{2} [^│]+│ | {14}│ | {14})")
        return "".join(prefix.sub("", line) for line in text.splitlines())

    def exchange(self, code, exchanges, setup=None, columns=80):
        test_home = tempfile.mkdtemp(prefix="ta-terminal-home-")
        if setup:
            setup(test_home)
        if callable(exchanges):
            exchanges = exchanges(test_home)
        previous_home = os.environ.get("HOME")
        os.environ["HOME"] = test_home
        pid, fd = pty.fork()
        if pid:
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, columns, 0, 0))
        if pid == 0:
            os.environ['TERM'] = 'xterm-256color'
            os.environ['INPUTRC'] = '/dev/null'
            os.execv(sys.executable, [sys.executable, '-c', code])
        output = b''
        cursor = 0
        deadline = time.monotonic() + 10
        try:
            for expected, payload in exchanges:
                while expected not in output[cursor:]:
                    if time.monotonic() >= deadline:
                        self.fail('terminal prompt timed out')
                    if select.select([fd], [], [], 0.1)[0]:
                        try:
                            output += os.read(fd, 8192)
                        except OSError:
                            break
                position = output.find(expected, cursor)
                self.assertGreaterEqual(position, 0, output.decode('utf-8', 'replace'))
                cursor = position + len(expected)
                os.write(fd, payload)
            while time.monotonic() < deadline:
                if select.select([fd], [], [], 0.1)[0]:
                    try:
                        chunk = os.read(fd, 8192)
                    except OSError:
                        break
                    if not chunk:
                        break
                    output += chunk
            else:
                self.fail('terminal process did not exit')
            return output.decode('utf-8', 'replace')
        finally:
            os.close(fd)
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(pid, 0)
            import shutil
            shutil.rmtree(test_home, ignore_errors=True)
            if previous_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = previous_home

    def test_backspace_delete_arrows_and_utf8(self):
        code = ('from termux_agent.cli import configure_readline; import json; '
                'assert configure_readline(); print("RESULT="+json.dumps(input("ta> ")))')
        for keys, expected in [(b'ab\x08c\n', 'ac'), (b'ab\x7fc\n', 'ac'),
                               (b'abc\x1b[D\x1b[3~\n', 'ab'),
                               (b'abc\x1b[D\x08\n', 'ac'),
                               ('中文'.encode() + b'\x08' + '字\n'.encode(), '中字')]:
            with self.subTest(keys=keys):
                text = self.exchange(code, [(b'ta> ', keys)])
                result = re.search(r'RESULT=(".*")', text)
                self.assertIsNotNone(result, text)
                self.assertEqual(json.loads(result.group(1)), expected)

    def test_real_cli_edits_commands_without_sending_them_to_model(self):
        code = ('import sys; from termux_agent.cli import main; '
                'sys.argv=["ta","--no-session","--no-agent"]; main()')
        prompt = "You     │ ".encode()
        text = self.exchange(code, [(prompt, b'/helpx\x08\n'),
                                   (prompt, b'!printf BANG_DIRECT\n'),
                                   (prompt, b'/exitX\x1b[D\x1b[3~\n')])
        self.assertIn('Show commands', text)
        self.assertIn('BANG_DIRECT', text)
        self.assertNotIn('Unknown command', text)
        self.assertNotIn('LLM 请求失败', text)

    def test_triple_quote_and_backslash_multiline_input(self):
        code = ('from termux_agent.cli import configure_readline,read_prompt; import json; '
                'assert configure_readline(); print("RESULT="+json.dumps(read_prompt("ta> ")))')
        text = self.exchange(code, [(b'ta> ', b'"""\n'), (b'... ', b'line one\n'),
                                    (b'... ', b'line two\n'), (b'... ', b'"""\n')])
        result = re.search(r'RESULT=(".*")', text)
        self.assertEqual(json.loads(result.group(1)), 'line one\nline two')
        text = self.exchange(code, [(b'ta> ', b'line one\\\n'), (b'... ', b'line two\n')])
        result = re.search(r'RESULT=(".*")', text)
        self.assertEqual(json.loads(result.group(1)), 'line one\nline two')

    def test_readline_history_is_private_and_api_keys_are_redacted(self):
        code = ('from termux_agent.cli import configure_readline,save_readline_history; '
                'import readline,os,pathlib; os.environ["TEST_API_KEY"]="termux-secret-123"; '
                'configure_readline(pathlib.Path.home()/".local/share/termux-agent/repl_history"); '
                'readline.add_history("ask termux-secret-123"); save_readline_history(); '
                'p=pathlib.Path.home()/".local/share/termux-agent/repl_history"; '
                'readline.clear_history(); readline.read_history_file(str(p)); '
                'print("RESULT="+str(oct(p.stat().st_mode & 0o777))+":"+readline.get_history_item(1))')
        text = self.exchange(code, [])
        self.assertIn('RESULT=0o600:ask [REDACTED]', text)
        self.assertNotIn('termux-secret-123', text)

    def test_status_refreshes_after_session_restore_api_switch_and_new_session(self):
        from pathlib import Path

        paths = {}
        def setup(home):
            from termux_agent.config import Config
            from termux_agent.sessions import SessionStore

            root = Path(home)
            directory_a, directory_b = root / "work-a", root / "work-b"
            directory_a.mkdir()
            directory_b.mkdir()
            paths.update(directory_a=directory_a, directory_b=directory_b)
            config_path = root / ".config/termux-agent/config.toml"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                'default_api="local"\n'
                '[apis.local]\nbase_url="http://127.0.0.1:8085/v1"\nmodel="model-A"\n'
                '[apis.cloud]\nbase_url="https://example.invalid/v1"\nmodel="model-B"\n',
                encoding="utf-8")
            selected_config = Config.load(config_path, "local")
            store = SessionStore(root / ".local/share/termux-agent")
            scope = {"directory": str(directory_b), "config_path": str(config_path),
                     "session_file": None, "disabled": True, "instructions": "",
                     "instruction_path": None}
            payload = {
                "version": 3,
                "config": selected_config.public_dict(),
                "scope": scope,
                "base_system": "test system prompt",
                "messages": [{"role": "system", "content": "test system prompt"}],
                "active_skills": {},
                "counters": {"_total_calls": 0, "_tool_calls_total": 0,
                             "_usage_history": [], "_request_history_chars": [],
                             "_conversation_count": 0, "_direct_shell_count": 0},
            }
            store.save("abcdef123456", 0, payload)
            store.close()

        code = '''import sys
from pathlib import Path
import termux_agent.agent as core
from termux_agent.cli import main
class FakeClient:
    def __init__(self, config):
        self.streamed = self.streamed_text = False
        self.on_stream = None
    def close(self): pass
core.Client = FakeClient
home = Path.home()
sys.argv = ["ta", "--config", str(home / ".config/termux-agent/config.toml"),
           "--directory", str(home / "work-a"), "--no-agent"]
main()
'''
        def exchanges(home):
            prompt = "You     │ ".encode()
            return [(prompt, b"/session load abcdef123456\n"),
                    (prompt, b"/api cloud\n"),
                    (prompt, b"/session new\n"),
                    (prompt, b"/exit\n")]

        text = self.exchange(code, exchanges, setup=setup, columns=160)
        rows = re.findall(r"Status\s+│ Session=([^ ]+) \| Model=([^ ]+) \| Cwd=([^\r\n]+)", text)
        self.assertTrue(rows, text)
        self.assertEqual([row[1] for row in rows], ["model-A", "model-A", "model-B", "model-B"], text)
        self.assertEqual(rows[1][0], "abcdef123456")
        self.assertNotEqual(rows[0][0], rows[2][0])
        self.assertNotEqual(rows[2][0], rows[3][0])
        self.assertEqual(len(rows), 4, text)
        self.assertEqual(rows[0][2], str(paths["directory_a"].resolve()))
        self.assertEqual(rows[1][2], str(paths["directory_b"]))
        self.assertNotIn("\x1b[", text)

    def test_titles_refresh_after_rename_new_session_and_restore_in_real_tty(self):
        from pathlib import Path
        def setup(home):
            path = Path(home) / '.config/termux-agent/config.toml'
            path.parent.mkdir(parents=True)
            path.write_text('model="m"\n')
        code = '''import sys
from pathlib import Path
import termux_agent.agent as core
import termux_agent.cli as cli
class FakeClient:
    def __init__(self, config): pass
    def chat(self, *args): raise AssertionError("No model calls for title commands")
    def close(self): pass
core.Client = FakeClient
identifiers = iter(["111111111111", "222222222222"])
cli.new_id = lambda: next(identifiers)
sys.argv = ["ta", "--directory", str(Path.home()), "--no-agent"]
cli.main()
'''
        prompt = 'You     │ '.encode()
        text = self.exchange(code, [(prompt, '/title 稿件审阅\n'.encode()),
                                    (prompt, '/sessions find 审阅\n'.encode()),
                                    (prompt, '/session new 第二会话\n'.encode()),
                                    (prompt, b'/session load 111111111111\n'),
                                    (prompt, b'/exit\n')], setup=setup, columns=100)
        self.assertGreaterEqual(text.count('Title=稿件审阅'), 2, text)
        self.assertIn('Title=第二会话', text)
        self.assertIn('1 saved sessions', text)
        self.assertIn('* 111111111111 · 稿件审阅', text)
        self.assertNotIn('LLM 请求失败', text)
        self.assertNotIn('Unknown command', text)

    def test_status_wraps_narrow_tty_and_is_absent_from_piped_cli(self):
        code = ('import sys; from termux_agent.cli import main; '
                'sys.argv=["ta", "--no-session", "--no-agent"]; main()')
        text = self.exchange(code, [("You: ".encode(), b"/exit\n")], columns=32)
        joined = re.sub(r"\r?\n {14}", "", text)
        self.assertIn("Status:", text)
        self.assertIn("Session=", joined)
        self.assertIn("(memory)", joined)
        self.assertIn("Model=Qwen3.8-27B-MTPLX-Speed", joined)
        self.assertIn("Cwd=", joined)
        self.assertNotIn("\x1b[", text)
        for line in text.splitlines():
            self.assertLessEqual(len(line), 32, line)

        with tempfile.TemporaryDirectory(prefix="ta-pipe-home-") as home:
            env = os.environ.copy()
            env["HOME"] = home
            env["TERM"] = "dumb"
            result = subprocess.run([sys.executable, "-c", code], input=b"/exit\n",
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    env=env, timeout=10, check=True)
        output = result.stdout.decode("utf-8", "replace")
        self.assertNotIn("Status", output)

    def test_tab_completes_slash_command_and_api_profile(self):
        from pathlib import Path
        def setup(home):
            path = Path(home) / ".config/termux-agent/config.toml"
            path.parent.mkdir(parents=True)
            path.write_text('default_api="local"\n[apis.local]\nbase_url="http://127.0.0.1:8085/v1"\nmodel="a"\n[apis.cloud]\nbase_url="https://example.com/v1"\nmodel="b"\n')
        code = ('import sys; from termux_agent.cli import main; '
                'sys.argv=["ta","--no-session","--no-agent"]; main()')
        prompt = "You     │ ".encode()
        text = self.exchange(code, [(prompt, b'/hel\t\n'),
                                   (prompt, b'/api cl\t\n'),
                                   (prompt, b'/exi\t\n')], setup=setup)
        self.assertIn('Show commands', text)
        self.assertIn('cloud', text)
        self.assertNotIn('Unknown command', text)

    def test_local_path_tab_completion_and_exact_text_submission(self):
        from pathlib import Path
        def setup(home):
            root = Path(home)
            (root / "docs").mkdir()
            (root / "docs/note.txt").write_text("FILE_SENTINEL")
            (root / "with space.txt").write_text("SPACE_SENTINEL")
        code = '''import sys
import termux_agent.agent as core
from termux_agent.cli import main
class FakeClient:
    last_usage = None
    last_request = None
    def __init__(self, config): pass
    def close(self): pass
    def chat(self, messages, tools):
        return {"content": "READ:" + messages[-1]["content"]}
core.Client = FakeClient
sys.argv = ["ta", "--no-session", "--no-agent"]
main()
'''
        prompt = "You     │ ".encode()
        def exchanges(home):
            return [(prompt, b'~/do\tno\t\n'),
                    (prompt, '～/with s\t\n'.encode()),
                    (prompt, (home + '/docs/no\t\n').encode()),
                    (prompt, b'/exi\t\n')]
        text = self.exchange(code, exchanges, setup=setup)
        self.assertEqual(text.count("READ:FILE_SENTINEL"), 2)
        self.assertEqual(text.count("READ:SPACE_SENTINEL"), 1)
        self.assertNotIn("No such file", text)

    def test_columns_wrap_chinese_and_keep_stream_roles_separate(self):
        from termux_agent.terminal_ui import Columns, _cells
        for width in (32, 60):
            out = io.StringIO()
            ui = Columns(out, enabled=True, width=lambda: width)
            ui.message("You", "中文内容" * 12)
            ui.feed("Think", "原因")
            ui.feed("Agent", "结果" * 16)
            ui.message("Tool", '{"name":"shell"}')
            text = out.getvalue()
            self.assertIn("Think", text)
            self.assertIn("Agent", text)
            self.assertIn("Tool", text)
            for line in text.splitlines():
                self.assertLessEqual(sum(_cells(c) for c in line), width)

    def test_message_timestamps_are_stable_per_stream_and_refresh_per_message(self):
        from termux_agent.terminal_ui import Columns

        out = io.StringIO()
        times = iter((datetime(2026, 10, 10, 12, 34, 59),
                      datetime(2026, 10, 10, 12, 35, 0)))
        ui = Columns(out, enabled=True, width=lambda: 52, clock=lambda: next(times))

        # Multiple chunks and wrapped lines are one streamed message, even if
        # wall-clock time changes while the stream is being rendered.
        ui.feed("Agent", "A" * 90)
        ui.feed("Agent", "B")
        ui.finish()
        ui.message("Agent", "next")

        text = out.getvalue()
        self.assertIn("12:34 Agent", text)
        self.assertIn("12:35 Agent", text)
        self.assertEqual(text.count("12:34"), 1, text)
        self.assertEqual(text.count("12:35"), 1, text)
        body = self.unwrap_timestamped(text)
        self.assertIn("A" * 90 + "B", body)
        self.assertIn("next", body)

    def test_message_timestamps_are_added_to_prompt_but_not_piped_output(self):
        from termux_agent.terminal_ui import Columns

        moment = datetime(2026, 10, 10, 8, 7)
        ui = Columns(io.StringIO(), enabled=True, width=lambda: 32,
                     clock=lambda: moment)
        self.assertIn("08:07", ui.prompt())
        self.assertIn("You", ui.prompt())

        out = io.StringIO()
        piped = Columns(out, enabled=False, clock=lambda: moment)
        piped.message("Agent", "plain output")
        self.assertEqual(out.getvalue(), "plain output\n")

    def test_timestamped_messages_fit_narrow_and_wide_terminal_widths(self):
        from termux_agent.terminal_ui import Columns, _cells

        moment = datetime(2026, 10, 10, 8, 7)
        for width in (20, 32, 80):
            with self.subTest(width=width):
                out = io.StringIO()
                ui = Columns(out, enabled=True, width=lambda: width,
                             clock=lambda: moment)
                ui.message("Status", "状态" * 20 + " Session=model-A | Cwd=/tmp")
                text = out.getvalue()
                self.assertIn("08:07", text)
                self.assertIn("Status", text)
                body = self.unwrap_timestamped(text)
                self.assertIn("Session=model-A", body)
                self.assertIn("Cwd=/tmp", body)
                for line in text.splitlines():
                    self.assertLessEqual(sum(_cells(char) for char in line), width,
                                         (width, line))

    def test_piped_stream_keeps_reasoning_and_answer_separate(self):
        from termux_agent.terminal_ui import Columns
        out = io.StringIO()
        ui = Columns(out, enabled=False)
        ui.feed("Think", "thought")
        ui.feed("Agent", "answer")
        ui.finish()
        self.assertEqual(out.getvalue(), "[reasoning] thought\nanswer\n")

    def test_completion_is_lazy_and_only_for_slash_commands(self):
        from termux_agent.terminal_ui import Completer
        class Input:
            def get_line_buffer(self): return ""
            def get_begidx(self): return 0
        calls = []
        completer = Completer(Input(), lambda: ["local", "cloud"],
                              lambda: (calls.append("skill") or ["termux-native-deploy"]),
                              lambda: ["loaded"], lambda: ["abcd1234abcd"],
                              lambda: __import__("pathlib").Path.cwd())
        self.assertEqual(completer.matches("/sk", 0, "/sk"), ["/skill"])
        self.assertEqual(completer.matches("hello", 0, "hello"), [])
        self.assertIn("load", completer.matches("/skill ", 7, ""))
        self.assertEqual(calls, [])
        self.assertEqual(completer.matches("/skill load ter", 12, "ter"),
                         ["termux-native-deploy"])
        self.assertEqual(calls, ["skill"])
        self.assertEqual(completer.matches("/api cl", 5, "cl"), ["cloud"])
        self.assertEqual(completer.matches("/session load ab", 14, "ab"),
                         ["abcd1234abcd"])
