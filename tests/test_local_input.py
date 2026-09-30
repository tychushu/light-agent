"""Explicit file submission must be exact, bounded, and distinct from commands."""
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from termux_agent.local_input import load_text_submission, path_value
from termux_agent.terminal_ui import COMMANDS, Completer


class LocalInputTests(unittest.TestCase):
    def test_home_unicode_space_names_bom_and_command_precedence(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {'HOME': td}):
            file = Path(td) / '中文 note.txt'
            file.write_bytes(b'\xef\xbb\xbf' + '内容\r\n/exit\n'.encode())
            for name in ('~/中文 note.txt', '～/中文 note.txt', '"' + str(file) + '"'):
                result = load_text_submission(name, COMMANDS)
                self.assertEqual(result.text, '内容\r\n/exit\n')
                self.assertEqual(result.path, file)
        self.assertIsNone(path_value('/help', COMMANDS))
        self.assertIsNone(path_value('/api local', COMMANDS))
        self.assertEqual(path_value('"/help"', COMMANDS), '/help')
        self.assertEqual(path_value('/help.txt', COMMANDS), '/help.txt')
        with self.assertRaises(ValueError): path_value('"/unclosed', COMMANDS)

    def test_invalid_files_are_rejected_without_partial_submission(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with self.assertRaises(ValueError): load_text_submission(str(root), COMMANDS)
            with self.assertRaises(FileNotFoundError): load_text_submission(str(root/'missing'), COMMANDS)
            file = root/'file'
            for data in (b'\0binary', b'\xff\xfe', b'  \n'):
                file.write_bytes(data)
                with self.assertRaises(ValueError): load_text_submission(str(file), COMMANDS)
            file.write_text('中文' * 10)
            self.assertEqual(load_text_submission(str(file), COMMANDS, 20).text, '中文' * 10)
            with self.assertRaises(ValueError): load_text_submission(str(file), COMMANDS, 19)
            fifo = root/'fifo'
            os.mkfifo(fifo)
            with self.assertRaises(ValueError): load_text_submission(str(fifo), COMMANDS)

    def test_path_completion_is_name_only_and_handles_spaces(self):
        class Input:
            def get_line_buffer(self): return ''
            def get_begidx(self): return 0
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {'HOME': td}):
            root = Path(td)
            (root/'docs').mkdir(); (root/'note.txt').write_text('text')
            (root/'with space.txt').write_text('text')
            scans = []
            complete = Completer(Input(), lambda: [], lambda: scans.append(1),
                                 lambda: [], lambda: [], lambda: root)
            self.assertEqual(complete.matches('~/no', 0, '~/no'), ['~/note.txt'])
            self.assertEqual(complete.matches('～/no', 0, '～/no'), ['～/note.txt'])
            self.assertEqual(complete.matches('~/do', 0, '~/do'), ['~/docs/'])
            self.assertEqual(complete.matches('~/with s', 7, 's'), ['space.txt'])
            prefix = str(root/'no')
            self.assertEqual(complete.matches(prefix, 0, prefix), [str(root/'note.txt')])
            self.assertEqual(scans, [])

    def test_cli_sends_exact_file_text_and_keeps_slash_commands(self):
        from termux_agent import cli
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {'HOME': td}):
            file = Path(td)/'note.txt'; file.write_text('/exit\nThis is a prompt from a file.\n')
            submitted = []
            class FakeAgent:
                def __init__(self, config, **kwargs):
                    self.config = config
                    self.client = type('C', (), {'close': lambda self: None})()
                def run(self, value):
                    submitted.append(value)
                    return 'ok'
            previous = Path.cwd()
            try:
                with patch.object(cli, 'Agent', FakeAgent), \
                     patch.object(cli.sys, 'argv', ['ta','--no-session','--no-agent','--directory',td]), \
                     patch('builtins.input', side_effect=['~/note.txt','/api','～/note.txt','/exit']), \
                     patch('sys.stdout', new_callable=io.StringIO):
                    cli.main()
            finally:
                os.chdir(previous)
            self.assertEqual(submitted, [file.read_text(), file.read_text()])
