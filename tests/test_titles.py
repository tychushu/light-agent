"""Manual session titles are metadata, not model input or conversation rewrites."""
import io
import os
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from termux_agent.agent import Agent
from termux_agent.config import Config
from termux_agent.sessions import SessionStore, new_id, snapshot, validate_title
from termux_agent.terminal_ui import Completer


class NoModelClient:
    calls = 0

    def __init__(self, *args):
        pass

    def chat(self, *args):
        NoModelClient.calls += 1
        raise AssertionError('Title management must not call a model')

    def close(self):
        pass


class TitleTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        home = patch.dict(os.environ, {'HOME': temp.name})
        home.start()
        self.addCleanup(home.stop)
        self.config = self.root / 'config.toml'
        self.config.write_text('auto_continue=false\n')
        self.folder = self.root / 'store'
        self.store = SessionStore(self.folder)
        self.addCleanup(self.store.close)
        agent = Agent(Config.load(self.config), client=NoModelClient(), base_system='test')
        agent.messages += [{'role': 'user', 'content': 'first\n\nmessage'}, {'role': 'assistant', 'content': 'answer'}]
        scope = {'directory': str(self.root), 'config_path': str(self.config), 'instructions': '',
                 'instruction_path': None, 'session_file': None, 'disabled': True}
        self.payload = snapshot(agent, scope)
        NoModelClient.calls = 0

    def run_cli(self, prompts, args=()):
        from termux_agent import cli
        out = io.StringIO()
        previous = Path.cwd()
        scope_args = [] if '--session' in args else ['--config', str(self.config), '--directory', str(self.root), '--no-agent']
        try:
            with patch.object(cli.sys, 'argv', ['ta', *scope_args, *args]), \
                    patch.object(cli.sys, 'stdout', out), patch('builtins.input', side_effect=prompts), \
                    patch('termux_agent.agent.Client', NoModelClient), \
                    patch.object(cli, 'SessionStore', lambda: SessionStore(self.folder)):
                cli.main()
        finally:
            os.chdir(previous)
        self.assertEqual(NoModelClient.calls, 0)
        return out.getvalue()

    def test_rename_preserves_payload_revision_and_other_writer_can_save(self):
        identifier = new_id()
        self.store.save(identifier, 0, self.payload)
        self.assertEqual(self.store.title(identifier), 'first message')
        before = self.store.load(identifier)
        updated = self.store.db.execute('SELECT updated FROM sessions WHERE id=?', (identifier,)).fetchone()[0]
        self.assertEqual(self.store.rename(identifier, '  小说校对  '), '小说校对')
        self.assertEqual(self.store.load(identifier), before)
        self.assertEqual(self.store.db.execute('SELECT updated FROM sessions WHERE id=?', (identifier,)).fetchone()[0], updated)
        other = SessionStore(self.folder)
        try:
            other.save(identifier, before[0], before[1])
        finally:
            other.close()
        self.assertEqual(self.store.title(identifier), '小说校对')
        self.assertIsNone(self.store.title('ffffffffffff'))

    def test_title_validation_and_literal_search(self):
        for invalid in (' ', 'x' * 121, 'a\nb', 'a\tb', 'a\x1bb', 'a\x85b', 'a\u2028b', None):
            with self.subTest(title=invalid), self.assertRaises(ValueError):
                validate_title(invalid)
        self.assertEqual(validate_title('中' * 120), '中' * 120)
        for title in ('章节规划_100%', 'Another Review', '另一会话'):
            identifier = new_id()
            self.store.save(identifier, 0, self.payload)
            self.store.rename(identifier, title)
        self.assertEqual([row['title'] for row in self.store.listing(query='规划')], ['章节规划_100%'])
        self.assertEqual(len(self.store.listing(query='%')), 1)
        self.assertEqual(len(self.store.listing(query='_')), 1)
        self.assertEqual([row['title'] for row in self.store.listing(query='review')], ['Another Review'])
        self.assertEqual(self.store.listing(query="' OR 1=1 --"), [])

    def test_title_lookup_does_not_decode_conversation_payload(self):
        identifier = new_id()
        self.store.save(identifier, 0, self.payload)
        with patch('termux_agent.sessions.json.loads', side_effect=AssertionError('must not read payload')):
            self.assertEqual(self.store.title(identifier), 'first message')

    def test_cli_title_before_first_message_persists_and_resumes_without_model(self):
        output = self.run_cli(['/title 我的校对', '/title', '/session', '/sessions find 校对', '/exit'])
        rows = self.store.listing()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['title'], '我的校对')
        identifier = rows[0]['id']
        self.assertEqual(len(self.store.load(identifier)[1]['messages']), 1)
        self.assertIn('我的校对', output)
        output = self.run_cli(['/title', '/session rename 改名后', '/title', '/exit'], ['--session', identifier])
        self.assertIn('我的校对', output)
        self.assertIn('改名后', output)
        self.assertEqual(self.store.title(identifier), '改名后')
        self.assertEqual(len(self.store.load(identifier)[1]['messages']), 1)

    def test_cli_rename_other_session_does_not_load_or_rewrite_it(self):
        identifier = new_id()
        self.store.save(identifier, 0, self.payload)
        before = self.store.load(identifier)
        with patch.object(SessionStore, 'load', side_effect=AssertionError('must not load target conversation')):
            output = self.run_cli(['/title 当前会话', f'/session rename-id {identifier} 已归档',
                                   '/sessions find 已归档', '/title', '/exit'])
        self.assertIn('已归档', output)
        self.assertIn('当前会话', output)
        self.assertEqual(self.store.title(identifier), '已归档')
        self.assertEqual(self.store.load(identifier), before)

    def test_title_in_memory_mode_does_not_create_a_saved_session(self):
        output = self.run_cli(['/title 测试', '/title', '/exit'], ['--no-session'])
        self.assertIn('Persistence disabled', output)
        self.assertEqual(self.store.listing(), [])

    def test_temporarily_unavailable_title_lookup_does_not_break_cli(self):
        with patch.object(SessionStore, 'title', side_effect=sqlite3.OperationalError('busy')):
            output = self.run_cli(['/title 已保存', '/title', '/sessions find', '/exit'])
        self.assertIn('[unavailable]', output)
        self.assertIn('Use /sessions find TEXT', output)
        self.assertEqual(self.store.listing()[0]['title'], '已保存')

    def test_title_and_search_commands_and_rename_id_are_completable(self):
        completer = Completer(None, lambda: [], lambda: [], lambda: [],
                              lambda: ['abcdef123456'], lambda: self.root)
        with patch.object(completer, '_paths', return_value=[]):
            self.assertIn('/title', completer.matches('/ti', 0, '/ti'))
        self.assertEqual(completer.matches('/sessions f', 10, 'f'), ['find'])
        line = '/session rename-id ab'
        self.assertEqual(completer.matches(line, line.rfind(' ') + 1, 'ab'), ['abcdef123456'])
