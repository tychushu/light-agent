"""History viewing remains local when saved APIs or directories disappear."""
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from termux_agent.agent import Agent
from termux_agent.config import Config
from termux_agent.sessions import SessionStore, HistoryClient, restore, snapshot


class LocalClient:
    def close(self):
        pass


class HistoryAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root/'config.toml'
        self.config.write_text('base_url="https://old.example/v1"\nmodel="old-model"\napproval_policy="never"\n')
        agent = Agent(Config.load(self.config), client=LocalClient(), instructions='saved instruction')
        agent.messages += [{'role': 'user', 'content': 'archived question'},
                           {'role': 'assistant', 'content': 'archived answer', 'reasoning_content': 'keep thinking'}]
        self.scope = dict(directory=str(self.root), config_path=str(self.config), instructions='saved instruction',
                          session_file=None, disabled=False, instruction_path=None)
        self.payload = snapshot(agent, self.scope)
        self.identifier = 'abcdef123456'
        self.store = SessionStore(self.root/'store')
        self.store.save(self.identifier, 0, self.payload)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def row(self):
        return self.store.db.execute('SELECT * FROM sessions WHERE id=?', (self.identifier,)).fetchone()

    def builder(self, config, text, base_system=None, client=None):
        return Agent(config, instructions=text, base_system=base_system, client=client or LocalClient())

    def changed_config(self):
        self.config.write_text('base_url="https://new.example/v1"\nmodel="new-model"\n')

    def run_cli(self, args, prompts, client):
        from termux_agent import cli
        original = Path.cwd()
        out = io.StringIO()
        try:
            os.chdir(self.root)
            with patch.object(cli.sys, 'argv', ['ta', *args]), patch('builtins.input', side_effect=prompts), \
                    patch.object(cli.sys, 'stdout', out), patch('termux_agent.agent.Client', client), \
                    patch.object(cli, 'SessionStore', lambda: SessionStore(self.root/'store')):
                cli.main()
        finally:
            os.chdir(original)
        return out.getvalue()

    def test_changed_or_missing_api_restores_locally_without_credentials_or_execution(self):
        for config in ('base_url="https://new.example/v1"\nmodel="new-model"\n', 'default_api="deleted"\n', 'broken = ['):
            with self.subTest(config=config):
                self.config.write_text(config)
                before = copy.deepcopy(self.payload)
                cfg, agent, scope = restore(self.payload, self.builder, history_fallback=True)
                self.assertTrue(agent.history_only)
                self.assertIsInstance(agent.client, HistoryClient)
                self.assertEqual(cfg.model, 'old-model')
                self.assertEqual(cfg.api_key, '')
                self.assertEqual(agent.messages, before['messages'])
                self.assertFalse(agent._approved('shell', {'command': 'touch forbidden'}))
                for action in (lambda: agent.run('must not send'), lambda: agent.delegate('must not delegate')):
                    with self.assertRaises(ValueError): action()
                self.assertEqual(agent.messages, before['messages'])
                self.assertEqual(self.payload, before)

    def test_matching_api_still_resumes_and_changed_protocol_can_be_viewed(self):
        cfg, agent, _ = restore(self.payload, self.builder, history_fallback=True)
        self.assertFalse(agent.history_only)
        self.config.write_text('base_url="https://old.example/v1"\nmodel="old-model"\nwire_api="responses"\n')
        _, archived, _ = restore(self.payload, self.builder, history_fallback=True)
        self.assertTrue(archived.history_only)
        with self.assertRaises(ValueError):
            restore(self.payload, self.builder)

    def test_cli_load_history_export_and_exit_do_not_rewrite_archive_or_run_tools(self):
        self.changed_config()
        self.store.save_draft(self.identifier, 1, {'content': 'saved partial reply'})
        before = self.row()
        constructed = []
        class NoRequests:
            def __init__(self, cfg): constructed.append(cfg.model)
            def chat(self, *args): raise AssertionError('history must not reach a model')
            def close(self): pass
        export = self.root/'archive.json'
        marker = self.root/'forbidden'
        output = self.run_cli(['--config', str(self.config), '--directory', str(self.root), '--no-agent'],
                              [f'/session load {self.identifier}', '/history 100', '/recall archived',
                               f'/export json {export}', 'must not send', '/sub must not delegate',
                               f'!touch {marker}', '/session fork', '/session save', '/exit'], NoRequests)
        self.assertIn('历史查看模式', output)
        self.assertIn('archived answer', output)
        self.assertEqual(self.row(), before)
        self.assertEqual(self.store.draft(self.identifier)['content'], 'saved partial reply')
        self.assertEqual(json.loads(export.read_text())['payload'], self.payload)
        self.assertEqual(constructed, ['new-model'])
        self.assertFalse(marker.exists())

    def test_startup_resume_works_with_missing_config_and_missing_work_directory(self):
        payload = copy.deepcopy(self.payload)
        payload['scope']['directory'] = str(self.root/'removed-workspace')
        self.store.save(self.identifier, 1, payload)
        before = self.row()
        self.config.unlink()
        with patch('termux_agent.agent.Client') as network:
            output = self.run_cli(['--session', self.identifier], ['/history', '/exit'], network)
        network.assert_not_called()
        self.assertIn('历史查看模式', output)
        self.assertIn('原工作目录已不存在', output)
        self.assertIn('archived answer', output)
        self.assertEqual(self.row(), before)

    def test_explicit_api_switch_to_same_profile_starts_fresh_without_sending_archive(self):
        self.changed_config()
        before = self.row()
        requests = []
        class Echo:
            def __init__(self, cfg): self.config = cfg
            def chat(self, messages, tools):
                requests.append(copy.deepcopy(messages))
                return {'content': 'fresh answer'}
            def close(self): pass
        output = self.run_cli(['--session', self.identifier],
                              ['/api default', 'fresh request', '/exit'], Echo)
        self.assertIn('fresh answer', output)
        self.assertEqual(len(requests), 1)
        self.assertEqual([m['role'] for m in requests[0]], ['system', 'user'])
        self.assertEqual(requests[0][-1]['content'], 'fresh request')
        self.assertEqual(self.row(), before)
        self.assertEqual(len(self.store.listing()), 2)

    def test_reloading_same_id_resumes_normally_after_original_api_is_restored(self):
        self.changed_config()
        requests = []
        class Echo:
            def __init__(self, cfg): pass
            def chat(self, messages, tools):
                requests.append(copy.deepcopy(messages))
                return {'content': 'resumed answer'}
            def close(self): pass
        prompts = iter([f'/session load {self.identifier}', 'resume original provider', '/exit'])
        def prompt(*args):
            self.config.write_text('base_url="https://old.example/v1"\nmodel="old-model"\n')
            return next(prompts)
        output = self.run_cli(['--session', self.identifier], prompt, Echo)
        self.assertIn('resumed answer', output)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0][1]['content'], 'archived question')
