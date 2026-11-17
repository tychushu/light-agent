"""Regression tests for P0, P1, and P2 review optimizations."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from termux_agent import approval, security
from termux_agent.api_profiles import ProfileStore
from termux_agent.config import Config
from termux_agent.sessions import SessionStore, scrub_keys, snapshot
from termux_agent.tools import execute

try:
    import httpx
except ImportError:
    httpx = None


class ConfigValidationOptimizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config_path = self.root / "config.toml"

    def test_unknown_top_level_and_toml_api_key_are_rejected(self):
        self.config_path.write_text('unknown_option = 1\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'unknown configuration key'):
            Config.load(self.config_path)

        self.config_path.write_text('api_key = "sk-plain"\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'api_key_env'):
            Config.load(self.config_path)

    def test_unknown_profile_keys_and_profile_api_key_are_rejected(self):
        self.config_path.write_text(
            '[apis.cloud]\nbase_url = "https://example.test/v1"\nmodel = "m"\ntypo_key = true\n',
            encoding='utf-8',
        )
        with self.assertRaisesRegex(ValueError, 'unknown configuration key'):
            Config.load(self.config_path, 'cloud')

        self.config_path.write_text(
            '[apis.cloud]\nbase_url = "https://example.test/v1"\nmodel = "m"\napi_key = "sk-bad"\n',
            encoding='utf-8',
        )
        with self.assertRaisesRegex(ValueError, 'api_key_env'):
            Config.load(self.config_path, 'cloud')

        self.config_path.write_text('model = "m"\n', encoding='utf-8')
        profiles_dir = self.root / 'profiles'
        profiles_dir.mkdir()
        (profiles_dir / 'extra.toml').write_text(
            'base_url = "https://extra.test/v1"\nmodel = "m"\nrogue_key = 42\n',
            encoding='utf-8',
        )
        with self.assertRaisesRegex(ValueError, 'unknown configuration key'):
            ProfileStore(self.config_path).entries()

    def test_profile_can_override_previously_global_options_and_ignores_ui_language(self):
        self.config_path.write_text(
            'ui_language = "en"\n'
            'approval_policy = "on-risk"\n'
            'max_output_chars = 20000\n'
            'max_subagents = 3\n'
            'auto_continue = false\n'
            '[apis.cloud]\n'
            'base_url = "https://example.test/v1"\n'
            'model = "cloud-model"\n'
            'ui_language = "zh"\n'
            'approval_policy = "never"\n'
            'max_output_chars = 32000\n'
            'max_input_file_chars = 128000\n'
            'max_subagents = 1\n'
            'subagent_max_steps = 5\n'
            'auto_continue = true\n'
            'max_continuations = 4\n'
            'silent_writes = false\n',
            encoding='utf-8',
        )
        cfg = Config.load(self.config_path, 'cloud')
        self.assertEqual(cfg.ui_language, 'en')
        self.assertEqual(cfg.approval_policy, 'never')
        self.assertEqual(cfg.max_output_chars, 32000)
        self.assertEqual(cfg.max_input_file_chars, 128000)
        self.assertEqual(cfg.max_subagents, 1)
        self.assertEqual(cfg.subagent_max_steps, 5)
        self.assertTrue(cfg.auto_continue)
        self.assertEqual(cfg.max_continuations, 4)
        self.assertFalse(cfg.silent_writes)

    def test_cloud_plaintext_http_with_api_key_is_rejected(self):
        for public_url in (
            'http://api.example.com/v1',
            'http://8.8.8.8/v1',
            'http://[2606:4700:4700::1111]/v1',
            'http://[2001:4860:4860::8888]/v1',
            'http://134744072/v1',
            'http://0x08080808/v1',
        ):
            with self.subTest(public_url=public_url), self.assertRaisesRegex(ValueError, 'https://'):
                Config(base_url=public_url, api_kind='cloud', api_key='secret-key').validate()

        # Allowed: HTTPS cloud with key, unauthenticated cloud HTTP, local HTTP with key,
        # and LAN/Magpie gateways even when marked kind="cloud" or using LAN suffixes.
        Config(base_url='https://api.example.com/v1', api_kind='cloud', api_key='secret-key').validate()
        Config(base_url='http://api.example.com/v1', api_kind='cloud', api_key='').validate()
        Config(base_url='http://127.0.0.1:8080/v1', api_kind='local', api_key='secret-key').validate()
        for lan_url in (
            'http://192.168.86.245:3425/v1',
            'http://10.0.0.5:8080/v1',
            'http://100.64.1.2:8080/v1',
            'http://[fd00::1]/v1',
            'http://localhost:8080/v1',
            'http://magpie.lan:8080/v1',
            'http://magpie.local:8080/v1',
        ):
            with self.subTest(lan_url=lan_url):
                Config(base_url=lan_url, api_kind='cloud', api_key='magpie-key').validate()

        store = ProfileStore(self.config_path)
        with self.assertRaisesRegex(ValueError, 'https://'):
            store.add('unsafe-cloud', 'http://api.example.com/v1', 'm', api_key_env='CLOUD_KEY', kind='cloud')
        store.add('magpie-lan', 'http://192.168.86.10:8080/v1', 'm', api_key_env='MAGPIE_KEY', kind='cloud')


class UnifiedRedactionTests(unittest.TestCase):
    def setUp(self):
        p = patch.object(security, '_KEY_NAMES', {'TERMUX_AGENT_API_KEY', 'CUSTOM_REGISTERED_AUTH'})
        p.start()
        self.addCleanup(p.stop)

    def test_redact_text_and_scrub_keys_cover_tokens_secrets_and_registered_names(self):
        env = {
            'CUSTOM_REGISTERED_AUTH': 'reg-secret-value-1234',
            'GATEWAY_API_TOKEN': 'tok-secret-value-5678',
            'APP_SECRET': 'app-secret-value-9999',
            'SHORT_API_KEY': '1',
        }
        with patch.dict(os.environ, env, clear=True):
            text = (
                'auth=reg-secret-value-1234 token=tok-secret-value-5678 '
                'secret=app-secret-value-9999 extra=extra-key-0001 keep=1'
            )
            redacted = security.redact_text(text, ('extra-key-0001',))
            self.assertNotIn('reg-secret-value-1234', redacted)
            self.assertNotIn('tok-secret-value-5678', redacted)
            self.assertNotIn('app-secret-value-9999', redacted)
            self.assertNotIn('extra-key-0001', redacted)
            self.assertIn('keep=1', redacted)

            payload = {'nested': [{'msg': text}]}
            scrubbed = scrub_keys(payload, ('extra-key-0001',))
            self.assertIn('keep=1', scrubbed['nested'][0]['msg'])
            self.assertNotIn('tok-secret-value-5678', scrubbed['nested'][0]['msg'])
            self.assertIn('tok-secret-value-5678', payload['nested'][0]['msg'])

    def test_short_explicit_key_is_always_scrubbed(self):
        with patch.dict(os.environ, {}, clear=True):
            payload = {'messages': [{'role': 'user', 'content': 'token is k1'}]}
            scrubbed = scrub_keys(payload, ('k1',))
            self.assertNotIn('k1', scrubbed['messages'][0]['content'])
            self.assertIn('[REDACTED]', scrubbed['messages'][0]['content'])

    def test_scrub_keys_with_no_secrets_returns_independent_copy(self):
        with patch.dict(os.environ, {}, clear=True):
            source = {'messages': [{'role': 'user', 'content': 'hello'}]}
            copied = scrub_keys(source)
            copied['messages'][0]['content'] = 'changed'
            self.assertEqual(source['messages'][0]['content'], 'hello')


class ApprovalHardeningTests(unittest.TestCase):
    def test_destructive_git_unlink_and_attached_short_option_paths_prompt(self):
        risky = (
            'git checkout -- .',
            'git checkout -f main',
            'git restore .',
            'git restore --worktree src/app.py',
            'git restore -SW src/app.py',
            'git -C repo reset --hard',
            'git --config-env advice.detachedHead=TA_REVIEW_GIT_VALUE reset --hard',
            'git --config-env advice.detachedHead=TA_REVIEW_GIT_VALUE push --force origin HEAD',
            'git --attr-source status reset --hard HEAD',
            'git --attr-source status push --force origin HEAD',
            'git -C -- reset --hard HEAD',
            'git -C -- checkout -- .',
            'git --unknown-opt val reset --hard',
            'git stash drop',
            'git stash clear',
            'git branch -D feature',
            'git branch --delete -f feature',
            'unlink important.txt',
            'curl https://example.test/file -o~/.bashrc',
            'curl https://example.test/file -o.git/hooks/pre-commit',
        )
        for command in risky:
            with self.subTest(command=command):
                self.assertTrue(approval.needs_approval('shell', {'command': command}, 'on-risk'))

    def test_harmless_git_subcommands_do_not_prompt(self):
        safe = (
            'git status',
            'git checkout main',
            'git restore --staged file.py',
            'git restore -S file.py',
            'git stash list',
            'git branch -d merged',
            'git -C -- checkout main',
            'git log -- checkout',
        )
        for command in safe:
            with self.subTest(command=command):
                self.assertFalse(approval.needs_approval('shell', {'command': command}, 'on-risk'))


class ReadFileEofTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_line_mode_reports_eof_accurately(self):
        path = self.root / 'lines.txt'
        path.write_text(''.join(f'line {i}\n' for i in range(1, 11)), encoding='utf-8')

        partial = execute('read_file', {'path': str(path), 'start_line': 1, 'end_line': 5})
        self.assertFalse(partial['truncated'])
        self.assertFalse(partial['eof'])
        self.assertEqual(partial['next_line'], 6)

        exact_end = execute('read_file', {'path': str(path), 'start_line': 6, 'end_line': 10})
        self.assertFalse(exact_end['truncated'])
        self.assertTrue(exact_end['eof'])
        self.assertEqual(exact_end['next_line'], 11)

        past_end = execute('read_file', {'path': str(path), 'start_line': 12, 'end_line': 15})
        self.assertFalse(past_end['truncated'])
        self.assertTrue(past_end['eof'])

        capped = execute('read_file', {'path': str(path), 'start_line': 1, 'end_line': 10}, max_output_chars=40)
        self.assertTrue(capped['truncated'])
        self.assertFalse(capped['eof'])

    def test_byte_mode_reports_eof_accurately(self):
        path = self.root / 'bytes.txt'
        path.write_bytes(b'0123456789abcdefghijklmnopqrstuvwxyzABCD')

        partial = execute('read_file', {'path': str(path), 'offset': 0, 'limit': 16})
        self.assertFalse(partial['truncated'])
        self.assertFalse(partial['eof'])

        to_end = execute('read_file', {'path': str(path), 'offset': 16, 'limit': 24})
        self.assertFalse(to_end['truncated'])
        self.assertTrue(to_end['eof'])

        capped = execute('read_file', {'path': str(path), 'offset': 0, 'limit': 40}, max_output_chars=24)
        self.assertTrue(capped['truncated'])
        self.assertFalse(capped['eof'])


@unittest.skipIf(httpx is None, 'httpx unavailable')
class TokenEstimateCacheTests(unittest.TestCase):
    def test_estimate_text_tokens_caches_short_and_long_strings(self):
        from termux_agent.token_budget import _ESTIMATE_CACHE, estimate_text_tokens

        _ESTIMATE_CACHE.clear()
        short = 'hello world 缓存测试'
        long_text = 'alpha beta 测试数据 ' * 400
        self.assertGreater(len(long_text), 4096)

        first_short = estimate_text_tokens(short)
        first_long = estimate_text_tokens(long_text)
        with patch('termux_agent.token_budget._WORD') as matcher:
            self.assertEqual(estimate_text_tokens(short), first_short)
            self.assertEqual(estimate_text_tokens(long_text), first_long)
            matcher.findall.assert_not_called()


@unittest.skipIf(httpx is None or not hasattr(httpx, 'MockTransport'), 'httpx MockTransport unavailable')
class ClientAndAgentOptimizationTests(unittest.TestCase):
    def test_client_always_emits_fresh_wire_after_nested_in_place_mutations(self):
        from termux_agent.client import Client

        for wire_api in ('chat_completions', 'responses'):
            sent_bodies = []

            def respond(request):
                sent_bodies.append(json.loads(request.content))
                if wire_api == 'responses':
                    return httpx.Response(200, json={'status': 'completed', 'output': [
                        {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'ok'}]},
                    ]})
                return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': 'ok'}}]})

            cfg = Config(base_url='http://127.0.0.1:9/v1', model='wire-test', wire_api=wire_api, stream=False)
            http = httpx.Client(transport=httpx.MockTransport(respond))
            self.addCleanup(http.close)
            client = Client(cfg, http_client=http)

            part = {'type': 'text', 'text': 'initial-part'}
            fn_args = {'path': 'before.txt'}
            tool_params = {'type': 'object', 'properties': {'a': {'type': 'string'}}}
            tools = [{'type': 'function', 'function': {'name': 'demo', 'parameters': tool_params}}]
            assistant = {
                'role': 'assistant',
                'content': 'call',
                'tool_calls': [{'id': 'c1', 'type': 'function',
                                'function': {'name': 'demo', 'arguments': json.dumps(fn_args)}}],
            }
            if wire_api == 'responses':
                assistant['_responses_output'] = [
                    {'type': 'reasoning', 'summary': [], 'encrypted_content': 'enc-before'},
                    {'type': 'function_call', 'call_id': 'c1', 'name': 'demo', 'arguments': '{"path":"before.txt"}'},
                ]
            messages = [{'role': 'user', 'content': [part]}, assistant,
                        {'role': 'tool', 'tool_call_id': 'c1', 'content': 'ok'}]

            client.request_context(messages, tools)
            # Mutate nested structures in place without changing top-level object identities or list lengths.
            part['text'] = 'mutated-part'
            tool_params['properties']['a']['type'] = 'integer'
            assistant['tool_calls'][0]['function']['arguments'] = '{"path":"after.txt"}'
            if wire_api == 'responses':
                assistant['_responses_output'][0]['encrypted_content'] = 'enc-after'
                assistant['_responses_output'][1]['arguments'] = '{"path":"after.txt"}'

            client.chat(messages, tools)
            wire_text = json.dumps(sent_bodies[-1], ensure_ascii=False)
            self.assertIn('mutated-part', wire_text)
            self.assertIn('integer', wire_text)
            self.assertIn('after.txt', wire_text)
            if wire_api == 'responses':
                self.assertIn('enc-after', wire_text)
                self.assertNotIn('enc-before', wire_text)

    def test_debug_context_redacts_registered_token_secrets_and_short_explicit_keys(self):
        from termux_agent.agent import Agent
        from termux_agent.client import Client

        for secret in ('primary-key-9999', 'k1'):
            with self.subTest(secret=secret):
                cfg = Config(base_url='http://127.0.0.1:9/v1', model='m', api_key=secret,
                             token_count_mode='off', stream=False)
                http = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={})))
                self.addCleanup(http.close)
                with patch.dict(os.environ, {'OTHER_API_TOKEN': 'secondary-tok-8888'}, clear=False):
                    agent = Agent(cfg, client=Client(cfg, http_client=http))
                    agent.messages.append({'role': 'user', 'content': f'use {secret} and secondary-tok-8888'})
                    debug = agent.debug_context()
                serialized = json.dumps(debug)
                self.assertNotIn(secret, serialized)
                self.assertNotIn('secondary-tok-8888', serialized)

    def test_llama_cpp_compaction_verifies_candidate_with_bounded_tokenize_calls(self):
        from termux_agent.agent import Agent
        from termux_agent.client import Client

        tokenize_calls = []

        def respond(request):
            path = request.url.path
            if path.endswith('/props'):
                return httpx.Response(200, json={'n_ctx': 3600})
            if path.endswith('/apply-template'):
                return httpx.Response(404, text='unsupported')
            if path.endswith('/tokenize'):
                payload = json.loads(request.content)
                tokenize_calls.append(len(payload['content']))
                # Proportional token count so full history triggers compaction and compact fits.
                count = max(1, len(payload['content']) // 4)
                return httpx.Response(200, json={'tokens': [1] * count})
            raise AssertionError(path)

        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {'HOME': td}):
            cfg = Config(base_url='http://127.0.0.1:9/v1', model='local-m', api_kind='local',
                         token_count_mode='local', output_reserve_tokens=200,
                         auto_compact=True, auto_compact_threshold=0.75, auto_compact_keep_groups=6)
            http = httpx.Client(transport=httpx.MockTransport(respond))
            self.addCleanup(http.close)
            agent = Agent(cfg, client=Client(cfg, http_client=http))
            from termux_agent.token_budget import TokenCounter
            agent.token_counter = TokenCounter(cfg, http_client=http, cache_path=Path(td) / 'tok.json')
            self.addCleanup(agent.close)
            agent.messages = [{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': 'goal'}]
            for idx in range(10):
                cid = f'call_{idx}'
                agent.messages.append({
                    'role': 'assistant', 'content': '',
                    'tool_calls': [{'id': cid, 'type': 'function',
                                    'function': {'name': 'read_file', 'arguments': json.dumps({'path': f'f{idx}.txt'})}}],
                })
                agent.messages.append({'role': 'tool', 'tool_call_id': cid, 'content': 'x' * 700})

            tokenize_calls.clear()
            compacted = agent._compact_for_context(rules='')
            self.assertTrue(compacted)
            # 1 preflight (messages+tools = 2) + 3 unique candidates (tools cached, so 3 message-only tokenize calls)
            # instead of 14 calls without tool-token caching and candidate deduplication.
            self.assertLessEqual(len(tokenize_calls), 5)

    def test_cli_load_session_calls_snapshot_once(self):
        from termux_agent import cli

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg_path = root / 'config.toml'
            cfg_path.write_text('base_url="http://127.0.0.1:9/v1"\nmodel="m"\ntoken_count_mode="off"\n',
                                encoding='utf-8')
            store_root = root / 'store'
            store = SessionStore(store_root)
            try:
                cfg = Config.load(cfg_path)
                from termux_agent.agent import Agent
                from termux_agent.client import Client
                with httpx.Client() as http:
                    a = Agent(cfg, client=Client(cfg, http_client=http))
                    a.messages.append({'role': 'user', 'content': 'saved turn'})
                    scope = {'directory': str(root), 'config_path': str(cfg_path),
                             'session_file': None, 'disabled': True,
                             'instructions': '', 'instruction_path': None}
                    store.save('111111111111', 0, snapshot(a, scope))
            finally:
                store.close()

            snap_calls = []
            orig_snapshot = cli.snapshot

            def counting_snapshot(*args, **kwargs):
                snap_calls.append(1)
                return orig_snapshot(*args, **kwargs)

            prev_cwd = Path.cwd()
            try:
                with patch.dict(os.environ, {'HOME': str(root)}), \
                     patch.object(cli, 'SessionStore', lambda: SessionStore(store_root)), \
                     patch.object(cli, 'snapshot', side_effect=counting_snapshot), \
                     patch.object(cli.sys, 'argv', ['ta', '--session', '111111111111']), \
                     patch('builtins.input', side_effect=[EOFError()]), \
                     patch('builtins.print'):
                    cli.main()
            finally:
                os.chdir(prev_cwd)
            # load_session builds 1 snapshot (instead of 2); exit finally save_current builds 1.
            self.assertEqual(len(snap_calls), 2)


if __name__ == '__main__':
    unittest.main()
