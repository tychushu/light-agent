"""Focused completion behavior for numbered interactive session selection."""
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from termux_agent.terminal_ui import COMMANDS, Completer


class InputState:
    def __init__(self, line='', begin=0):
        self.line = line
        self.begin = begin

    def get_line_buffer(self):
        return self.line

    def get_begidx(self):
        return self.begin


class SessionSelectionUI(unittest.TestCase):
    def completer(self, directory, choices=None, ids=None):
        calls = []
        def session_choices():
            calls.append('choices')
            return ['1', '2', '10'] if choices is None else choices

        def session_ids():
            calls.append('ids')
            return ['abcdef123456', '123456abcdef'] if ids is None else ids

        completer = Completer(
            InputState(), lambda: [], lambda: [], lambda: [], session_ids,
            lambda: Path(directory), session_choices=session_choices)
        return completer, calls

    def test_resume_command_is_recognized_without_affecting_bare_text(self):
        completer, calls = self.completer('.')
        self.assertIn('/resume', completer.matches('/re', 0, '/re'))
        self.assertIn('/recall', completer.matches('/re', 0, '/re'))
        self.assertEqual(completer.matches('123', 0, '123'), [])
        self.assertEqual(calls, [])

    def test_resume_and_session_load_complete_cached_ordinals_and_ids_lazily(self):
        completer, calls = self.completer('.')
        self.assertEqual(calls, [])
        self.assertCountEqual(
            completer.matches('/resume ', 8, ''),
            ['1', '2', '10', 'abcdef123456', '123456abcdef'])
        self.assertEqual(calls, ['choices', 'ids'])
        calls.clear()
        self.assertCountEqual(
            completer.matches('/session load 1', 14, '1'),
            ['1', '10', '123456abcdef'])
        self.assertEqual(calls, ['choices', 'ids'])

    def test_delete_and_rename_id_keep_id_only_completion(self):
        completer, calls = self.completer('.')
        self.assertCountEqual(
            completer.matches('/session delete ab', 16, 'ab'), ['abcdef123456'])
        self.assertCountEqual(
            completer.matches('/session rename-id 1', 19, '1'), ['123456abcdef'])
        self.assertEqual(calls, ['ids', 'ids'])

    def test_selection_callbacks_are_not_used_for_subcommands_or_paths(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / 'note.txt').write_text('x', encoding='utf-8')
            with patch.dict(os.environ, {'HOME': td}):
                completer, calls = self.completer(root)
                self.assertIn('load', completer.matches('/session ', 9, ''))
                self.assertEqual(completer.matches('/help', 0, '/help'), ['/help'])
                self.assertEqual(completer.matches('~/note.txt', 0, '~/note.txt'),
                                 ['~/note.txt'])
                self.assertEqual(calls, [])

    def test_existing_constructor_and_readline_call_remain_compatible(self):
        state = InputState('/resume ', 8)
        completer = Completer(state, lambda: [], lambda: [], lambda: [],
                              lambda: ['abcdef123456'], lambda: Path('.'),
                              session_choices=lambda: ['1'])
        self.assertEqual(completer('', 0), '1')
        self.assertIn(completer('', 1), ('abcdef123456', None))


if __name__ == '__main__':
    unittest.main()
