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
import tempfile
import termios
import time
import unittest


class TerminalTests(unittest.TestCase):
    def exchange(self, code, exchanges, setup=None, columns=80):
        test_home = tempfile.mkdtemp(prefix="ta-terminal-home-")
        if setup:
            setup(test_home)
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
                'print("RESULT="+str(oct(p.stat().st_mode & 0o777))+":"+p.read_text().strip())')
        text = self.exchange(code, [])
        self.assertIn('RESULT=0o600:ask [REDACTED]', text)
        self.assertNotIn('termux-secret-123', text)

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
