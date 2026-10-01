import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from termux_agent.agent import Agent
from termux_agent.config import Config
from termux_agent.sessions import SessionStore, new_id, snapshot, restore, validate_messages


class FakeClient:
    def close(self):
        pass


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = self.root / 'config.toml'
        self.config.write_text('base_url="https://example.com/v1"\nmodel="test-model"\n')
        self.agent = Agent(Config.load(self.config), client=FakeClient(), instructions='saved rule')
        self.agent.messages += [{'role': 'user', 'content': 'hello'},
                                {'role': 'assistant', 'content': 'hi', 'reasoning_content': 'keep this'}]
        self.scope = dict(directory=str(self.root), config_path=str(self.config), instructions='saved rule',
                          session_file=None, disabled=False, instruction_path=None)
        self.store = SessionStore(self.root / 'store')

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_roundtrip_permissions_redaction_and_stats(self):
        self.agent.config.api_key = 'secret-for-test'
        self.agent.messages[-1]['content'] = 'secret-for-test'
        self.agent._total_calls = 2
        self.agent._usage_history = [{'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12}]
        payload = snapshot(self.agent, self.scope)
        self.assertNotIn('secret-for-test', json.dumps(payload))
        identifier = new_id()
        self.assertEqual(self.store.save(identifier, 0, payload), 1)
        revision, saved = self.store.load(identifier)
        config, agent, scope = restore(saved, lambda cfg, text, base_system=None: Agent(cfg, client=FakeClient(), instructions=text, base_system=base_system))
        self.assertEqual(revision, 1)
        self.assertEqual(agent.messages[-1]['reasoning_content'], 'keep this')
        self.assertEqual(agent.stats()['api_calls_total'], 2)
        self.assertEqual(agent.system, self.agent.system)
        self.assertEqual((self.root/'store/sessions.sqlite3').stat().st_mode & 0o777, 0o600)

    def test_concurrent_changes_delete_and_rename(self):
        identifier = new_id()
        payload = snapshot(self.agent, self.scope)
        self.store.save(identifier, 0, payload)
        other = SessionStore(self.root / 'store')
        try:
            other.save(identifier, 1, payload)
            with self.assertRaises(ValueError):
                self.store.save(identifier, 1, payload)
            self.store.rename(identifier, 'name')
            self.assertEqual(self.store.listing()[0]['title'], 'name')
            self.store.delete(identifier)
            with self.assertRaises(ValueError):
                other.save(identifier, 2, payload)
        finally:
            other.close()

    def test_changed_endpoint_or_model_is_rejected(self):
        payload = snapshot(self.agent, self.scope)
        self.config.write_text('base_url="https://different.example/v1"\nmodel="test-model"\n')
        with self.assertRaises(ValueError):
            restore(payload, lambda *args: self.fail('must not construct client'))

    def test_tool_history_preserved_and_incomplete_rejected(self):
        calls = [{'id': 'a', 'type': 'function', 'function': {'name': 'shell', 'arguments': '{}'}}]
        self.agent.messages += [{'role': 'assistant', 'content': None, 'tool_calls': calls,
                                 'reasoning_content': 'reason'}]
        with self.assertRaises(ValueError):
            snapshot(self.agent, self.scope)
        self.agent.messages += [{'role': 'tool', 'tool_call_id': 'a', 'content': '{}'}]
        validate_messages(snapshot(self.agent, self.scope)['messages'])
        with self.assertRaises(ValueError):
            self.store.load('../bad-id')

    def test_cli_save_resume_clear_history_and_delete(self):
        from termux_agent import cli
        class EchoClient:
            last_usage = None
            last_request = None
            def __init__(self, cfg):
                pass
            def chat(self, messages, tools):
                return {'content': 'hello saved', 'reasoning_content': 'retained'}
            def close(self):
                pass
        original = Path.cwd()
        def run(args, prompts):
            try:
                with patch.object(cli.sys, 'argv', ['ta', *args]), patch('builtins.input', side_effect=prompts), patch('builtins.print'), patch('termux_agent.agent.Client', EchoClient), patch.object(cli, 'SessionStore', lambda: SessionStore(self.root / 'cli-store')):
                    cli.main()
            finally:
                os.chdir(original)
        run(['--config', str(self.config), '--directory', str(self.root)], ['hello', '/session rename First', '/exit'])
        db = SessionStore(self.root/'cli-store')
        identifier = db.listing()[0]['id']
        (self.root/'agent.md').write_text('x' * 16001)
        os.chdir(self.root)
        run(['--session', identifier], ['/history', 'second turn', '/clear', '/session new Second', '/exit'])
        first = db.load(identifier)[1]
        self.assertEqual(len([m for m in first['messages'] if m['role']=='user']), 2)
        self.assertEqual(first['messages'][-1]['reasoning_content'], 'retained')
        second = next(row['id'] for row in db.listing() if row['title']=='Second')
        run(['--session', second], [f'/session delete {identifier} --yes', '/exit'])
        with self.assertRaises(ValueError):
            db.load(identifier)
        db.close()
