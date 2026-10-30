"""Core session selection uses exact cached metadata rows and the existing loader."""
import copy
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from termux_agent.agent import Agent
from termux_agent.config import Config
from termux_agent.sessions import SessionStore, snapshot


class FakeClient:
    requests = []
    allow_chat = False

    def __init__(self, config=None):
        self.config = config

    def chat(self, messages, tools):
        if not self.allow_chat:
            raise AssertionError('session selection must not call a model')
        self.requests.append(copy.deepcopy(messages))
        return {'content': 'fake answer'}

    def close(self):
        pass


class FakeReadline:
    def __init__(self):
        self.completer = None

    def get_current_history_length(self):
        return 0

    def set_completer_delims(self, value):
        pass

    def set_completer(self, value):
        self.completer = value

    def parse_and_bind(self, value):
        pass


class CapturingCompleter:
    instances = []

    def __init__(self, readline, profiles, skills, active_skills, session_ids,
                 directory, session_choices=None):
        self.session_choices = session_choices
        self.instances.append(self)


class InputHook:
    def __init__(self, action):
        self.action = action


class FakeOutput(io.StringIO):
    def __init__(self, tty):
        super().__init__()
        self.tty = tty

    def isatty(self):
        return self.tty


class SessionSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.original_cwd = Path.cwd()
        self.home = patch.dict(os.environ, {'HOME': str(self.root)})
        self.home.start()
        self.config = self.root / 'config.toml'
        self.config.write_text('base_url="https://old.example/v1"\nmodel="old-model"\n'
                               'approval_policy="never"\nauto_continue=false\n')
        self.store = SessionStore(self.root / 'store')
        agent = Agent(Config.load(self.config), client=FakeClient(), base_system='test')
        agent.messages += [{'role': 'user', 'content': 'archived question'},
                           {'role': 'assistant', 'content': 'archived answer'}]
        self.payload = snapshot(agent, {
            'directory': str(self.root), 'config_path': str(self.config),
            'instructions': '', 'instruction_path': None, 'session_file': None,
            'disabled': True,
        })

    def tearDown(self):
        try:
            self.store.close()
        finally:
            os.chdir(self.original_cwd)
        self.home.stop()
        self.temp.cleanup()

    def add_session(self, identifier, title, updated):
        self.store.save(identifier, 0, copy.deepcopy(self.payload))
        self.store.rename(identifier, title)
        self.store.db.execute('UPDATE sessions SET updated=? WHERE id=?', (updated, identifier))
        self.store.db.commit()

    def set_order(self, *identifiers):
        for index, identifier in enumerate(identifiers):
            self.store.db.execute('UPDATE sessions SET updated=? WHERE id=?',
                                  (2_000_000_000 - index, identifier))
        self.store.db.commit()

    def run_cli(self, inputs, *, tty=False, args=(), allow_chat=False):
        from termux_agent import cli
        output = FakeOutput(tty)
        prompts = []
        restore_calls = []
        FakeClient.requests = []
        FakeClient.allow_chat = allow_chat
        CapturingCompleter.instances.clear()
        readline = FakeReadline()
        iterator = iter(inputs)

        def fake_input(prompt=''):
            prompts.append(prompt)
            item = next(iterator)
            if isinstance(item, InputHook):
                item.action()
                return fake_input(prompt)
            if isinstance(item, BaseException):
                raise item
            return item

        real_restore = cli.restore

        def counting_restore(*call_args, **call_kwargs):
            restore_calls.append(call_args[0].get('messages', [])[-1])
            return real_restore(*call_args, **call_kwargs)

        scope_args = [] if '--session' in args else [
            '--config', str(self.config), '--directory', str(self.root), '--no-agent']
        previous = Path.cwd()
        try:
            with patch.object(cli.sys, 'argv', ['ta', *scope_args, *args]), \
                    patch.object(cli.sys, 'stdout', output), \
                    patch.object(cli.sys, 'stdin', io.StringIO()), \
                    patch.object(cli.sys.stdin, 'isatty', return_value=tty), \
                    patch('builtins.input', side_effect=fake_input), \
                    patch('termux_agent.agent.Client', FakeClient), \
                    patch.object(cli, 'SessionStore', lambda: SessionStore(self.root / 'store')), \
                    patch.object(cli, 'Completer', CapturingCompleter), \
                    patch.object(cli, 'configure_readline', return_value=True), \
                    patch.object(cli, 'restore', counting_restore), \
                    patch.object(cli, '_readline', readline), \
                    patch.object(cli, '_history_file', None):
                cli.main()
        finally:
            os.chdir(previous)
        choices = (CapturingCompleter.instances[-1].session_choices()
                   if CapturingCompleter.instances else None)
        return output.getvalue(), prompts, choices, restore_calls

    def test_cached_ordinal_is_immutable_and_completion_is_lazy(self):
        self.add_session('aaaaaaaaaaaa', 'A', 1_900_000_000)
        self.add_session('bbbbbbbbbbbb', 'B', 1_800_000_000)
        self.set_order('aaaaaaaaaaaa', 'bbbbbbbbbbbb')

        def reorder():
            self.set_order('bbbbbbbbbbbb', 'aaaaaaaaaaaa')

        output, _, choices, restore_calls = self.run_cli(
            ['/sessions', InputHook(reorder), '/resume 1', '/exit'])
        self.assertEqual(choices, ['1', '2'])
        self.assertIn('"id": "aaaaaaaaaaaa"', output)
        self.assertEqual(len(restore_calls), 1)

    def test_filtered_cache_only_maps_rows_actually_displayed(self):
        self.add_session('aaaaaaaaaaaa', 'Alpha', 1_900_000_000)
        self.add_session('bbbbbbbbbbbb', 'Beta', 1_800_000_000)
        output, _, choices, _ = self.run_cli(
            ['/sessions find Beta', '/resume 2', '/resume 1', '/exit'])
        self.assertEqual(choices, ['1'])
        self.assertIn('会话序号超出最后一次显示的列表范围', output)
        self.assertIn('"id": "bbbbbbbbbbbb"', output)

    def test_full_numeric_id_is_resolved_before_ordinal(self):
        self.add_session('123456789012', 'Numeric ID', 1_800_000_000)
        self.add_session('abcdef123456', 'Ordinal one', 1_900_000_000)
        self.set_order('abcdef123456', '123456789012')
        output, _, _, restore_calls = self.run_cli(
            ['/sessions', '/session load 1', '/resume 123456789012', '/exit'])
        self.assertIn('"id": "123456789012"', output)
        self.assertEqual(len(restore_calls), 2)

    def test_sessions_count_caches_only_visible_rows_and_never_loads(self):
        self.add_session('aaaaaaaaaaaa', 'A', 1_700_000_000)
        self.add_session('bbbbbbbbbbbb', 'B', 1_800_000_000)
        self.add_session('cccccccccccc', 'C', 1_900_000_000)
        output, _, choices, restore_calls = self.run_cli(['/sessions 2', '/resume 3', '/exit'])
        self.assertEqual(choices, ['1', '2'])
        self.assertIn('会话序号超出最后一次显示的列表范围', output)
        self.assertEqual(restore_calls, [])

    def test_numeric_resume_without_prior_listing_never_queries_silently(self):
        self.add_session('aaaaaaaaaaaa', 'A', 1_900_000_000)
        with patch.object(SessionStore, 'listing', side_effect=AssertionError('must not query')):
            output, _, choices, restore_calls = self.run_cli(['/resume 1', '/exit'])
        self.assertIn('先用 /sessions 或 /resume 查看会话', output)
        self.assertEqual(choices, [])
        self.assertEqual(restore_calls, [])

    def test_current_session_is_a_noop_unless_history_reload_is_required(self):
        self.add_session('aaaaaaaaaaaa', 'Current', 1_900_000_000)
        self.set_order('aaaaaaaaaaaa')
        output, _, _, restore_calls = self.run_cli(
            ['/sessions', '/resume 1', '/exit'], args=['--session', 'aaaaaaaaaaaa'])
        self.assertIn('"id": "aaaaaaaaaaaa"', output)
        self.assertEqual(len(restore_calls), 1)

    def test_offline_history_fallback_allows_resume_without_model_requests(self):
        self.add_session('aaaaaaaaaaaa', 'Offline', 1_900_000_000)
        self.config.write_text('base_url="https://new.example/v1"\nmodel="new-model"\n'
                               'approval_policy="never"\nauto_continue=false\n')
        output, _, _, restore_calls = self.run_cli(
            ['/resume', '1', '/history', '/exit'], tty=True,
            args=['--session', 'aaaaaaaaaaaa'])
        self.assertEqual(len(restore_calls), 2)
        self.assertIn('历史查看模式', output)
        self.assertIn('archived answer', output)
        self.assertEqual(FakeClient.requests, [])

    def test_tty_picker_success_selects_displayed_number(self):
        self.add_session('aaaaaaaaaaaa', 'A', 1_800_000_000)
        self.add_session('bbbbbbbbbbbb', 'B', 1_900_000_000)
        self.set_order('bbbbbbbbbbbb', 'aaaaaaaaaaaa')
        output, prompts, _, restore_calls = self.run_cli(
            ['/resume', '2', '/exit'], tty=True)
        self.assertIn('选择序号或ID（回车取消）', prompts[1])
        self.assertIn('Session=aaaaaaaaaaaa', output)
        self.assertEqual(len(restore_calls), 1)

    def test_tty_picker_blank_eof_and_ctrl_c_cancel_without_exiting(self):
        self.add_session('aaaaaaaaaaaa', 'A', 1_900_000_000)
        for cancelled in ('', EOFError(), KeyboardInterrupt()):
            with self.subTest(cancelled=type(cancelled).__name__ if isinstance(cancelled, BaseException) else 'blank'):
                output, prompts, _, restore_calls = self.run_cli(
                    ['/resume', cancelled, '/exit'], tty=True)
                self.assertIn('已取消会话选择', output)
                self.assertEqual(len(prompts), 3)
                self.assertEqual(restore_calls, [])

    def test_nontty_resume_lists_and_guides_without_consuming_next_command(self):
        self.add_session('aaaaaaaaaaaa', 'A', 1_900_000_000)
        output, prompts, _, restore_calls = self.run_cli(['/resume', '/exit'])
        self.assertIn('"id": "aaaaaaaaaaaa"', output)
        self.assertIn('输入 /resume 序号或 ID', output)
        self.assertNotIn('选择序号或ID', prompts)
        self.assertEqual(len(prompts), 2)
        self.assertEqual(restore_calls, [])

    def test_empty_resume_is_clear_and_does_not_enter_picker(self):
        output, prompts, _, restore_calls = self.run_cli(['/resume', '/exit'], tty=True)
        self.assertIn('没有已保存会话', output)
        self.assertNotIn('选择序号或ID', prompts)
        self.assertEqual(len(prompts), 2)
        self.assertEqual(restore_calls, [])

    def test_deleted_cached_id_fails_safely_and_keeps_active_context(self):
        self.add_session('aaaaaaaaaaaa', 'Current', 1_900_000_000)
        self.add_session('bbbbbbbbbbbb', 'Deleted', 1_800_000_000)
        self.set_order('aaaaaaaaaaaa', 'bbbbbbbbbbbb')

        def delete_cached():
            self.store.delete('bbbbbbbbbbbb')

        output, _, _, restore_calls = self.run_cli(
            ['/sessions', InputHook(delete_cached), '/resume 2', '/session', '/exit'],
            args=['--session', 'aaaaaaaaaaaa'])
        self.assertIn('未找到会话', output)
        self.assertIn('"id": "aaaaaaaaaaaa"', output)
        self.assertEqual(len(restore_calls), 1)

    def test_bare_numeric_chat_is_not_session_selection(self):
        self.add_session('aaaaaaaaaaaa', 'A', 1_900_000_000)
        output, _, _, restore_calls = self.run_cli(['123', '/exit'], allow_chat=True)
        self.assertEqual(FakeClient.requests[-1][-1]['content'], '123')
        self.assertIn('fake answer', output)
        self.assertEqual(restore_calls, [])

    def test_resume_without_session_persistence_is_clear(self):
        output, _, _, _ = self.run_cli(['/resume', '/exit'], args=['--no-session'])
        self.assertIn('--no-session 已关闭会话持久化', output)


if __name__ == '__main__':
    unittest.main()
