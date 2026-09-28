"""Exercise real terminal input bytes, not mocked Python input()."""
import json
import os
import pty
import re
import select
import signal
import sys
import time
import unittest


class TerminalTests(unittest.TestCase):
    def exchange(self, code, exchanges):
        pid, fd = pty.fork()
        if pid == 0:
            os.environ['TERM'] = 'xterm-256color'
            os.environ['INPUTRC'] = '/dev/null'
            os.execv(sys.executable, [sys.executable, '-c', code])
        output = b''
        deadline = time.monotonic() + 10
        try:
            for expected, payload in exchanges:
                start = len(output)
                while expected not in output[start:]:
                    if time.monotonic() >= deadline:
                        self.fail('terminal prompt timed out')
                    if select.select([fd], [], [], 0.1)[0]:
                        output += os.read(fd, 8192)
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
        text = self.exchange(code, [(b'ta> ', b'/helpx\x08\n'),
                                   (b'ta> ', b'/exitX\x1b[D\x1b[3~\n')])
        self.assertIn('Show commands', text)
        self.assertNotIn('Unknown command', text)
        self.assertNotIn('LLM 请求失败', text)
