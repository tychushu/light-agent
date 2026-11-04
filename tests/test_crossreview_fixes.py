"""Second-review guards. Destructive examples are classified, never executed."""
import copy
import io
import json
import os
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import patch

from termux_agent import security, tools
from termux_agent.agent import Agent
from termux_agent.approval import needs_approval
from termux_agent.config import Config
from termux_agent.model_input import project_tool_messages
from termux_agent.output_compression import FilterResult, run_rtk_filter
from termux_agent.sessions import SessionStore, has_unknown_tool_results
import test_mimo_acceptance as fixtures
from test_proactive_context import history
from test_subagents import ScriptClient


class CrossReviewSecurityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for name, value in (('_KEY_NAMES', {'TERMUX_AGENT_API_KEY'}), ('_FILES', set()), ('_DIRECTORIES', set())):
            p = patch.object(security, name, value); p.start(); self.addCleanup(p.stop)
        p = patch.dict(os.environ, {'HOME': str(self.root)}); p.start(); self.addCleanup(p.stop)

    def test_agent_credentials_hooks_and_data_require_approval_on_risk(self):
        for path in ('~/.config/termux-agent/config.toml', '~/.config/termux-agent/profiles/cloud.toml',
                     '~/.config/termux-agent/credentials.env', '~/.local/share/termux-agent/sessions.sqlite3',
                     '~/.ssh/config', '.git/hooks/pre-commit'):
            for name in ('write_file', 'edit_file'):
                with self.subTest(path=path, name=name):
                    args = {'path': path, 'content': 'text', 'new_text': 'text'}
                    self.assertTrue(needs_approval(name, args, 'on-risk'))
                    self.assertFalse(needs_approval(name, args, 'never'))
            for command in (f'cp source {path}', f'printf value > {path}', f'> {path}', f'ln -sf source {path}'):
                self.assertTrue(needs_approval('shell', {'command': command}, 'on-risk'), command)

    def test_custom_config_file_is_protected_without_protecting_entire_project(self):
        config = self.root / 'custom settings.toml'; config.write_text('')
        Config.load(config)
        self.assertTrue(needs_approval('write_file', {'path': str(config), 'content': 'text'}, 'on-risk'))
        self.assertTrue(needs_approval('shell', {'command': 'cp source ' + shlex.quote(str(config))}, 'on-risk'))
        self.assertFalse(needs_approval('write_file', {'path': str(self.root / 'ordinary.txt'), 'content': 'text'}, 'on-risk'))

    def test_custom_session_directory_is_protected(self):
        root = self.root / 'custom-history'
        store = SessionStore(root); self.addCleanup(store.close)
        self.assertTrue(needs_approval('write_file', {'path': str(root / 'sessions.sqlite3'), 'content': 'text'}, 'on-risk'))

    def test_protected_target_through_symlink_is_recognized(self):
        target = self.root / 'private_keys'; target.mkdir()
        (self.root / '.ssh').symlink_to(target, target_is_directory=True)
        alias = self.root / 'alias'; alias.symlink_to(target, target_is_directory=True)
        self.assertTrue(needs_approval('write_file', {'path': str(alias / 'config'), 'content': 'text'}, 'on-risk'))

    def test_additional_interpreters_redirects_and_destructive_commands(self):
        commands = ['> ~/.bashrc', '2> ~/.zshrc', 'ln -sf other ~/.bashrc', 'cp source "$TARGET"',
                    'perl -e "unlink q(example)"', 'node -e "process.exit()"', 'ruby -e "puts 1"',
                    "awk 'BEGIN{system(\"example\")}'", 'printf script | python3', 'python3 -u',
                    'git push --force origin main', 'git push --force-with-lease origin main',
                    'git push origin +HEAD:main', 'rsync --delete source/ destination/',
                    'rsync --delete-after source/ destination/', 'shred example', 'crontab -r', 'pm uninstall example']
        for command in commands:
            with self.subTest(command=command):
                self.assertTrue(needs_approval('shell', {'command': command}, 'on-risk'))
                self.assertFalse(needs_approval('shell', {'command': command}, 'never'))

    def test_all_profile_key_names_are_stripped_without_modifying_parent_environment(self):
        config = self.root / 'config.toml'
        config.write_text('api_key_env="BASE_CUSTOM_TOKEN"\n[apis.cloud]\n'
                          'base_url="https://example.test/v1"\nmodel="test"\napi_key_env="CLOUD_CUSTOM_TOKEN"\n')
        folder = self.root / 'profiles'; folder.mkdir()
        (folder / 'extra.toml').write_text('base_url="https://extra.test/v1"\nmodel="test"\napi_key_env="EXTRA_CUSTOM_TOKEN"\n')
        values = {'BASE_CUSTOM_TOKEN': 'base-secret-value-0123456789', 'CLOUD_CUSTOM_TOKEN': 'cloud-secret-value-0123456789',
                  'EXTRA_CUSTOM_TOKEN': 'extra-secret-value-0123456789', 'OTHER_API_KEY': 'other-secret-value-0123456789',
                  'KEY_ALIAS': 'cloud-secret-value-0123456789', 'KEEP_ME': 'ordinary', 'BASH_ENV': '/private/startup', 'ENV': '/private/startup'}
        with patch.dict(os.environ, values):
            Config.load(config)
            before = dict(os.environ)
            child = security.child_environment()
            for name in values:
                if name != 'KEEP_ME': self.assertNotIn(name, child)
            self.assertEqual(child['KEEP_ME'], 'ordinary')
            self.assertEqual(child['HOME'], str(self.root))
            self.assertEqual(os.environ, before)

    def test_short_placeholder_values_do_not_strip_unrelated_variables(self):
        security.register_key_names(('LOCAL_PLACEHOLDER',))
        for value in ('1', 'none', 'x' * 15, 'x' * 16):
            with self.subTest(value=value), patch.dict(os.environ, {
                    'LOCAL_PLACEHOLDER': value, 'UNRELATED_VALUE': value}):
                child = security.child_environment()
                self.assertNotIn('LOCAL_PLACEHOLDER', child)
                if len(value) < 16:
                    self.assertEqual(child['UNRELATED_VALUE'], value)
                else:
                    self.assertNotIn('UNRELATED_VALUE', child)

    def test_unknown_programs_with_protected_paths_require_approval(self):
        path = '~/.local/share/termux-agent/sessions.sqlite3'
        for command in (f'sqlite3 {path} "delete from sessions"', f'custom-editor {path}',
                        'custom-editor --file=~/.config/termux-agent/config.toml',
                        'rg --pre=custom-script pattern ~/.config/termux-agent/config.toml'):
            with self.subTest(command=command):
                self.assertTrue(needs_approval('shell', {'command': command}, 'on-risk'))
                self.assertFalse(needs_approval('shell', {'command': command}, 'never'))
        for name in ('cat', 'head', 'ls', 'grep', 'rg'):
            self.assertFalse(needs_approval('shell', {'command': f'{name} {path}'}, 'on-risk'))
        config = self.root / 'custom settings.toml'; config.write_text('')
        Config.load(config)
        command = 'custom-editor --file=' + shlex.quote(str(config))
        self.assertTrue(needs_approval('shell', {'command': command}, 'on-risk'))

    def test_direct_config_key_env_is_registered_before_shell_execution(self):
        config = Config(api_key_env='DIRECT_CUSTOM_TOKEN'); config.validate()
        with patch.dict(os.environ, {'DIRECT_CUSTOM_TOKEN': 'fake-secret'}), \
                patch('termux_agent.tools.subprocess.Popen', side_effect=OSError('not started')) as launch:
            tools.execute('shell', {'command': 'pwd'})
        self.assertNotIn('DIRECT_CUSTOM_TOKEN', launch.call_args.kwargs['env'])

    def test_browser_and_rtk_children_use_the_same_filtered_environment(self):
        security.register_key_names(('CUSTOM_PROVIDER_TOKEN',))
        with patch.dict(os.environ, {'CUSTOM_PROVIDER_TOKEN': 'fake-secret'}), \
                patch('termux_agent.tools._web_fetch_binary', return_value='/fake/lightpanda'), \
                patch('termux_agent.tools._web_fetch_process', return_value=(
                    b'{"http_status":200,"content":"ok"}', b'', 0)) as browser:
            tools.execute('web_fetch', {'url': 'https://example.test'})
            env = browser.call_args.args[1]
            self.assertNotIn('CUSTOM_PROVIDER_TOKEN', env)
            self.assertEqual(env['LIGHTPANDA_DISABLE_TELEMETRY'], 'true')
        with patch.dict(os.environ, {'CUSTOM_PROVIDER_TOKEN': 'fake-secret'}), \
                patch('termux_agent.output_compression._bounded_process', return_value=FilterResult(b'ok', 0)) as rtk:
            run_rtk_filter('/fake/rtk', 'pytest', b'input')
            self.assertNotIn('CUSTOM_PROVIDER_TOKEN', rtk.call_args.args[2])
            self.assertEqual(rtk.call_args.args[2]['RTK_TELEMETRY_DISABLED'], '1')


class CrossReviewProjectionTests(unittest.TestCase):
    def message(self, stdout='output'):
        return {'role': 'tool', 'tool_call_id': 'x', 'content': json.dumps({
            'stdout': stdout, 'exit_code': 0, 'output_compression': {'status': 'compressed',
            'raw_digest': 'a' * 64, 'before_bytes': 1000}})}

    def test_projection_is_parsed_once_until_content_changes(self):
        message = self.message(); before = copy.deepcopy(message); cache = {}
        original = json.loads
        with patch('termux_agent.model_input.json.loads', wraps=original) as load:
            first = project_tool_messages([message], cache)
            self.assertEqual(project_tool_messages([message], cache), first)
            self.assertEqual(load.call_count, 1)
            message['content'] = self.message('changed')['content']
            second = project_tool_messages([message], cache)
            self.assertEqual(load.call_count, 2)
        self.assertEqual(first[0]['tool_call_id'], before['tool_call_id'])
        self.assertNotEqual(first[0]['content'], second[0]['content'])
        self.assertIn('raw_digest', message['content'])

    def test_cache_releases_compacted_messages_and_keeps_live_metadata(self):
        message = self.message(); cache = {}
        project_tool_messages([message], cache)
        self.assertEqual(len(cache), 1)
        message['tool_call_id'] = 'new-id'
        self.assertEqual(project_tool_messages([message], cache)[0]['tool_call_id'], 'new-id')
        project_tool_messages([], cache)
        self.assertEqual(cache, {})


class CrossReviewResumeTests(unittest.TestCase):
    def test_recovery_notice_uses_structured_outcomes_not_read_file_body(self):
        def result(value):
            return [{'role': 'tool', 'content': json.dumps(value)}]
        self.assertFalse(has_unknown_tool_results(result({'content': 'outcome is unknown'})))
        self.assertTrue(has_unknown_tool_results(result({'execution_status': 'unknown'})))
        self.assertTrue(has_unknown_tool_results(result({'write_status': 'failed'})))
        self.assertTrue(has_unknown_tool_results(result({'error': 'Interrupted checkpoint: tool outcome is unknown. It may have run.'})))

    def test_compaction_branch_precedes_first_resume_checkpoint(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {'HOME': td}):
            a = Agent(Config(max_context_chars=40000, token_count_mode='off'),
                      client=ScriptClient({'content': 'done'}), base_system='rules')
            self.addCleanup(a.close)
            a.messages = history() + [{'role': 'user', 'content': 'latest human goal'}]
            a._ensure_task_state('latest human goal')
            events = []
            def branch(*args):
                events.append('branch'); return True
            def checkpoint():
                events.append('checkpoint')
                self.assertIn('branch', events, 'source would be checkpointed before the compaction branch')
            a.branch_after_compact = branch; a.checkpoint = checkpoint
            a.continue_task()
            self.assertEqual(events[0], 'branch')
            self.assertIn({'role': 'user', 'content': 'latest human goal'}, a.messages)


class CrossReviewDurableSessions(fixtures.AcceptanceTestCase):
    def test_completed_checkpoint_load_does_not_warn_or_rewrite_row(self):
        config = self.root / 'config.toml'
        config.write_text('token_count_mode="off"\n')
        store_root = self.root / 'store'
        self.run_cli(['--config', str(config), '--directory', str(self.root), '--no-agent'],
                     ['task', '/exit'], [{'content': 'done'}], store=store_root)
        with fixtures.open_store(store_root) as store:
            identifier = store.ids()[0]
            before = store.db.execute('SELECT * FROM sessions WHERE id=?', (identifier,)).fetchone()
            payload = store.load(identifier)[1]
            self.assertTrue(payload['checkpoint'])
            self.assertEqual(payload['last_run']['status'], 'complete')
        output, _ = self.run_cli(['--session', identifier], ['/exit'], store=store_root, bomb_network=True)
        self.assertNotIn('恢复了未完成任务的检查点', output)
        with fixtures.open_store(store_root) as store:
            self.assertEqual(before, store.db.execute('SELECT * FROM sessions WHERE id=?', (identifier,)).fetchone())
