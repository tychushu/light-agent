"""File payloads stay out of tool displays; disk verification remains truthful."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from termux_agent.agent import Agent
from termux_agent.config import Config
from termux_agent.display_privacy import hide_write_payloads, safe_tool_arguments
from termux_agent.tools import execute


class SilentWriteTests(unittest.TestCase):
    def test_write_edit_verify_disk_hash_without_returning_text(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td)/'file.txt'
            text = 'WRITE_SENTINEL 中文\n'
            result = execute('write_file', {'path': str(path), 'content': text})
            self.assertEqual(path.read_text(), text)
            self.assertTrue(result['verified'])
            self.assertEqual(result['sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertNotIn(text, json.dumps(result, ensure_ascii=False))
            edited = execute('edit_file', {'path': str(path), 'old_text': 'WRITE_SENTINEL', 'new_text': 'EDIT_SENTINEL'})
            self.assertTrue(edited['verified'])
            self.assertEqual(edited['replacements'], 1)
            self.assertEqual(edited['sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertNotIn('EDIT_SENTINEL', json.dumps(edited))

    def test_verification_failure_is_not_reported_as_success(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td)/'file.txt'
            fake = MagicMock(); fake.hexdigest.return_value = '0' * 64
            with patch('termux_agent.tools.hashlib.file_digest', return_value=fake):
                result = execute('write_file', {'path': str(path), 'content': 'written'})
            self.assertFalse(result['verified'])
            self.assertIn('error', result)
            self.assertEqual(path.read_text(), 'written')

    def test_diagnostic_copy_hides_valid_and_malformed_arguments(self):
        call = {'function': {'name': 'write_file', 'arguments': json.dumps({'path': '/x', 'content': 'HIDDEN_BODY'})}}
        context = {'messages': [{'role': 'assistant', 'tool_calls': [call]}]}
        before = copy.deepcopy(context)
        displayed = hide_write_payloads(context)
        self.assertNotIn('HIDDEN_BODY', json.dumps(displayed))
        self.assertIn('body_hidden', json.dumps(displayed))
        self.assertEqual(context, before)
        broken = {'name': 'edit_file', 'arguments': 'HIDDEN_BROKEN_JSON'}
        self.assertNotIn('HIDDEN_BROKEN_JSON', json.dumps(hide_write_payloads(broken)))
        self.assertEqual(safe_tool_arguments('shell', {'command': 'echo ok'}), {'command': 'echo ok'})

    def test_cli_approval_events_debug_and_history_do_not_echo_write_body(self):
        from termux_agent import cli
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {'HOME': td}):
            root = Path(td); target = root/'file.txt'
            config = root/'config.toml'
            config.write_text('base_url="https://example.com/v1"\nmodel="m"\napproval_policy="always"\n')
            clients = []
            bodies = ('BODY_WRITE_SENTINEL', 'BODY_EDIT_SENTINEL')
            class FakeClient:
                last_usage = None
                last_request = None
                def __init__(self, config): self.step = 0; clients.append(self)
                def close(self): pass
                def chat(self, messages, tools):
                    self.last_request = {'messages': copy.deepcopy(messages), 'tools': tools}
                    self.step += 1
                    if self.step == 1:
                        name, args = 'write_file', {'path': str(target), 'content': bodies[0]}
                    elif self.step == 2:
                        name, args = 'edit_file', {'path': str(target), 'old_text': bodies[0], 'new_text': bodies[1]}
                    else:
                        return {'content': 'Saved.'}
                    return {'tool_calls': [{'id': str(self.step), 'type': 'function',
                                           'function': {'name': name, 'arguments': json.dumps(args)}}]}
            original = Path.cwd()
            try:
                with patch('termux_agent.agent.Client', FakeClient), \
                     patch.object(cli.sys, 'argv', ['ta','--config',str(config),'--directory',td,'--no-session','--no-agent']), \
                     patch.object(cli.sys.stdin, 'isatty', return_value=True), \
                     patch('builtins.input', side_effect=['save sample','y','y','/debug','/debug-context','/history','/exit']), \
                     patch('sys.stdout', new_callable=io.StringIO) as output:
                    cli.main()
                    rendered = output.getvalue()
            finally:
                os.chdir(original)
            for body in bodies: self.assertNotIn(body, rendered)
            self.assertIn('approval_required', rendered)
            self.assertIn('body_hidden', rendered)
            self.assertIn('sha256', rendered)
            self.assertIn('"verified": true', rendered)
            self.assertEqual(target.read_text(), bodies[1])
            self.assertIn(bodies[0], json.dumps(clients[0].last_request))
            self.assertIn(bodies[1], json.dumps(clients[0].last_request))

    def test_approval_denial_still_prevents_write_and_raw_debug_is_opt_in(self):
        class Client:
            last_request = {'messages': [{'tool_calls': [{'function': {'name': 'write_file',
                'arguments': '{"path":"/x","content":"RAW_SENTINEL"}'}}]}]}
            def close(self): pass
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {'HOME': td}):
            agent = Agent(Config(approval_policy='always'), client=Client(), approve=lambda *_: False)
            self.assertFalse(agent._approved('write_file', {'path': '/x', 'content': 'RAW_SENTINEL'}))
            self.assertNotIn('RAW_SENTINEL', json.dumps(agent.debug_context()))
            agent.config.silent_writes = False
            self.assertIn('RAW_SENTINEL', json.dumps(agent.debug_context()))
