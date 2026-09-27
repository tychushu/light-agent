import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import httpx
from termux_agent.config import Config
from termux_agent.agent import Agent
from termux_agent.client import Client
from termux_agent.instructions import load_instructions


class ProfilesTests(unittest.TestCase):
    def test_cloud_profile_credentials_and_request(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'config.toml'
            p.write_text('''[apis.cloud]
base_url = "https://api.example.com/v1"
model = "cloud-model"
api_key_env = "TEST_CLOUD_KEY"
''')
            with patch.dict(os.environ, {'TEST_CLOUD_KEY': 'cloud-secret', 'TERMUX_AGENT_API_KEY': 'local-secret'}):
                cfg = Config.load(p, 'cloud')
                self.assertEqual(cfg.api_key, 'cloud-secret')
                self.assertNotIn('cloud-secret', json.dumps(cfg.public_dict()))
                with self.assertRaises(ValueError):
                    Config.load(p, 'missing')
                seen = []
                def respond(req):
                    seen.append(req)
                    return httpx.Response(200, json={'choices': [{'message': {'content': 'ok'}}]})
                with httpx.Client(transport=httpx.MockTransport(respond)) as http:
                    agent = Agent(cfg, client=Client(cfg, http), instructions='session rule')
                    self.assertEqual(agent.run('hello'), 'ok')
                    self.assertEqual(str(seen[0].url), 'https://api.example.com/v1/chat/completions')
                    self.assertEqual(seen[0].headers['Authorization'], 'Bearer cloud-secret')
                    self.assertNotIn('local-secret', str(seen[0].headers))
                    agent.clear()
                    self.assertIn('session rule', agent.messages[0]['content'])
                    self.assertEqual(len(agent.messages), 1)
            p.write_text('[apis.public]\nbase_url="https://example.com/v1"\nmodel="m"\n')
            with patch.dict(os.environ, {'TERMUX_AGENT_API_KEY': 'local-secret'}):
                self.assertEqual(Config.load(p, 'public').api_key, '')

    def test_instruction_scope_and_limits(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / 'agent.md').write_text('parent must not load')
            child = root / 'project'
            child.mkdir()
            self.assertEqual(load_instructions(child), ('', None))
            (child / 'agent.md').write_text('directory rule')
            self.assertEqual(load_instructions(child)[0], 'directory rule')
            (child / 'session.md').write_text('session override')
            self.assertEqual(load_instructions(child, 'session.md')[0], 'session override')
            self.assertEqual(load_instructions(child, disabled=True), ('', None))
            with patch.object(Path, 'home', return_value=child):
                self.assertEqual(load_instructions(child), ('', None))
            (child / 'agent.md').write_text('x' * 16001)
            with self.assertRaises(ValueError):
                load_instructions(child)

    def test_cli_api_switch_does_not_forward_history(self):
        from termux_agent import cli
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'c.toml'
            p.write_text('[apis.cloud]\nbase_url="https://example.com/v1"\nmodel="m"\n')
            instances = []
            class FakeAgent:
                def __init__(self, config, **kwargs):
                    self.config, self.prompts = config, []
                    self.client = type('C', (), {'close': lambda self: None})()
                    instances.append(self)
                def run(self, text):
                    self.prompts.append(text)
                    return 'ok'
            original = Path.cwd()
            try:
                with patch.object(cli, 'Agent', FakeAgent), patch.object(cli.sys, 'argv', ['ta', '--no-session', '--config', str(p), '--directory', td]), patch('builtins.input', side_effect=['local-private', '/api missing', '/api cloud', 'cloud-prompt', '/exit']), patch('builtins.print'):
                    cli.main()
            finally:
                os.chdir(original)
            self.assertEqual(len(instances), 2)
            self.assertEqual(instances[0].prompts, ['local-private'])
            self.assertEqual(instances[1].prompts, ['cloud-prompt'])
            self.assertEqual(instances[1].config.api_profile, 'cloud')

class RouterTests(unittest.TestCase):
    def test_reasoning_and_user_agent_survive_tool_roundtrip(self):
        seen = []
        reasoning = 'provider reasoning must remain verbatim'
        def reply(req):
            body = json.loads(req.content)
            seen.append(body)
            self.assertEqual(req.headers['User-Agent'], 'claude-cli/2.1.119 (external, cli)')
            if len(seen) == 1:
                msg = {'role': 'assistant', 'content': None, 'reasoning_content': reasoning,
                       'tool_calls': [{'id': 'one', 'type': 'function', 'function': {'name': 'read_file', 'arguments': '{"path":"/missing-ta-test"}'}}]}
            else:
                self.assertEqual(body['messages'][2]['reasoning_content'], reasoning)
                self.assertEqual(body['messages'][3]['tool_call_id'], 'one')
                msg = {'role': 'assistant', 'content': 'done', 'reasoning_content': 'final reasoning'}
            return httpx.Response(200, json={'choices': [{'message': msg}]})
        config = Config(user_agent='claude-cli/2.1.119 (external, cli)')
        with httpx.Client(transport=httpx.MockTransport(reply)) as http:
            a = Agent(config, client=Client(config, http))
            self.assertEqual(a.run('test'), 'done')
            self.assertEqual(a.messages[-1]['reasoning_content'], 'final reasoning')
