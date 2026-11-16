"""Explicit model discovery uses only the requested profile and local CLI state."""
from contextlib import contextmanager
import copy
import io
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import httpx

from termux_agent.client import Client, ClientError, NetworkError, ProviderError
from termux_agent.config import Config
from termux_agent.i18n import help_text
from termux_agent.terminal_ui import Completer


class ModelDiscoveryTests(unittest.TestCase):
    @contextmanager
    def client(self, respond, **settings):
        config = Config(base_url='https://models.example/v1/', model='old-model', **settings)
        with httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=True) as http:
            yield Client(config, http_client=http)

    def test_both_wire_apis_use_authenticated_get_and_preserve_chat_diagnostics(self):
        for wire in ('responses', 'chat_completions'):
            with self.subTest(wire=wire):
                requests = []

                def respond(request):
                    requests.append(request)
                    return httpx.Response(200, json={'data': [{'id': 'z'}, {'id': 'a'}, {'id': 'a'}]})

                with self.client(respond, wire_api=wire, api_key='profile-secret', user_agent='discovery-test') as client:
                    self.assertEqual(requests, [])
                    client.last_request = {'model': 'old-model', 'messages': []}
                    client.last_attempts = 3
                    client.last_accepted = True
                    client.last_usage = {'total_tokens': 12}
                    before = copy.deepcopy((client.last_request, client.last_attempts, client.last_accepted, client.last_usage))
                    self.assertEqual(client.list_models(), ['a', 'z'])
                    self.assertEqual((client.last_request, client.last_attempts,
                                      client.last_accepted, client.last_usage), before)
                self.assertEqual(len(requests), 1)
                request = requests[0]
                self.assertEqual((request.method, str(request.url)), ('GET', 'https://models.example/v1/models'))
                self.assertEqual(request.headers['authorization'], 'Bearer profile-secret')
                self.assertEqual(request.headers['user-agent'], 'discovery-test')
                self.assertEqual(request.content, b'')

    def test_each_call_refreshes_the_catalog_and_empty_catalog_is_valid(self):
        calls = []

        def respond(request):
            calls.append(request)
            return httpx.Response(200, json={'data': [] if len(calls) == 1 else [{'id': 'new-model'}]})

        with self.client(respond) as client:
            self.assertEqual(client.list_models(), [])
            self.assertEqual(client.list_models(), ['new-model'])
        self.assertEqual(len(calls), 2)
        self.assertNotIn('authorization', calls[0].headers)

    def test_pagination_fetches_all_pages_with_stable_ids(self):
        cursors = []

        def respond(request):
            cursors.append(request.url.params.get('after'))
            if len(cursors) == 1:
                return httpx.Response(200, json={'data': [{'id': 'second'}], 'has_more': True, 'last_id': 'second'})
            return httpx.Response(200, json={'data': [{'id': 'first'}, {'id': 'second'}], 'has_more': False})

        with self.client(respond) as client:
            self.assertEqual(client.list_models(), ['first', 'second'])
        self.assertEqual(cursors, [None, 'second'])

    def test_invalid_schema_ids_and_json_are_rejected_without_echoing_secrets(self):
        values = [[], {}, {'data': {}}, {'data': [None]}, {'data': [{'id': 3}]},
                  {'data': [{'id': ''}]}, {'data': [{'id': 'bad\nmodel'}]},
                  {'data': [{'id': 'x' * 1025}]}, {'data': [{'id': 'profile-secret'}]}]
        for value in values:
            with self.subTest(value=value):
                with self.client(lambda request: httpx.Response(200, json=value), api_key='profile-secret') as client:
                    with self.assertRaises(ProviderError) as caught:
                        client.list_models()
                self.assertNotIn('profile-secret', str(caught.exception))
        with self.client(lambda request: httpx.Response(200, content=b'not JSON profile-secret')) as client:
            with self.assertRaisesRegex(ProviderError, 'invalid JSON'):
                client.list_models()

    def test_http_errors_and_error_envelopes_use_redacted_diagnostics(self):
        for status in (200, 401, 403, 404, 405, 429, 500):
            with self.subTest(status=status):
                def respond(request):
                    return httpx.Response(status, json={'error': {'code': 'denied', 'message': 'private profile-secret'}})
                with self.client(respond, api_key='profile-secret') as client:
                    with self.assertRaises(ClientError) as caught:
                        client.list_models()
                self.assertNotIn('profile-secret', str(caught.exception))
                self.assertIn('[REDACTED]', str(caught.exception))

    def test_html_and_redirects_do_not_leak_credentials_or_follow_other_hosts(self):
        for status in (200, 302):
            with self.subTest(status=status):
                requests = []

                def respond(request):
                    requests.append(request)
                    return httpx.Response(status, text='<html>profile-secret</html>',
                                          headers={'content-type': 'text/html', 'location': 'https://other.example/models'})

                with self.client(respond, api_key='profile-secret') as client:
                    with self.assertRaises(ClientError) as caught:
                        client.list_models()
                self.assertEqual(len(requests), 1)
                self.assertNotIn('profile-secret', str(caught.exception))

    def test_catalog_limit_applies_across_pages(self):
        calls = []

        def respond(request):
            calls.append(request)
            return httpx.Response(200, json={'data': [{'id': str(len(calls))}], 'padding': 'x' * 600,
                                            'has_more': True, 'last_id': str(len(calls))})

        with self.client(respond, max_response_bytes=1024) as client:
            with self.assertRaisesRegex(ProviderError, 'max_response_bytes'):
                client.list_models()
        self.assertEqual(len(calls), 2)

    def test_repeated_missing_or_unbounded_cursors_fail_with_no_partial_list(self):
        for more, cursor, expected_calls in ((True, None, 1), ('true', 'cursor', 1), (True, 'cursor', 2)):
            with self.subTest(more=more, cursor=cursor):
                requests = []

                def respond(request):
                    requests.append(request)
                    return httpx.Response(200, json={'data': [], 'has_more': more, 'last_id': cursor})

                with self.client(respond) as client:
                    with self.assertRaisesRegex(ProviderError, 'pagination'):
                        client.list_models()
                self.assertEqual(len(requests), expected_calls)
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={'data': [], 'has_more': True, 'last_id': str(len(requests))})

        with self.client(respond) as client:
            with self.assertRaisesRegex(ProviderError, '20 pages'):
                client.list_models()
        self.assertEqual(len(requests), 20)

    def test_transport_and_body_read_errors_close_the_response_without_secret_echo(self):
        class BrokenBody(httpx.SyncByteStream):
            closed = False

            def __iter__(self):
                yield b'{"data":'
                raise httpx.ReadTimeout('profile-secret')

            def close(self):
                self.closed = True

        body = BrokenBody()
        with self.client(lambda request: httpx.Response(200, stream=body)) as client:
            with self.assertRaisesRegex(NetworkError, 'ReadTimeout') as caught:
                client.list_models()
        self.assertTrue(body.closed)
        self.assertNotIn('profile-secret', str(caught.exception))

        def fail(request):
            raise httpx.ConnectError('profile-secret')

        with self.client(fail) as client:
            with self.assertRaisesRegex(NetworkError, 'ConnectError'):
                client.list_models()


class ModelDiscoveryCliTests(unittest.TestCase):
    def run_cli(self, inputs, respond, language='zh'):
        from termux_agent import cli

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / 'config.toml'
            config.write_text(f'''default_api = "local"
ui_language = "{language}"
[apis.local]
base_url = "http://127.0.0.1:8080/v1"
model = "local-model"
api_key_env = "LOCAL_KEY"
[apis.cloud]
base_url = "https://cloud.example/v1"
model = "unlisted-model"
api_key_env = "CLOUD_KEY"
wire_api = "responses"
''')
            before = config.read_bytes()
            agents = []
            requests = []
            queried = []

            class FakeAgent:
                def __init__(self, config, **kwargs):
                    self.config = config
                    self.client = SimpleNamespace(close=lambda: None)
                    self.prompts = []
                    agents.append(self)

                def run(self, value):
                    self.prompts.append(value)
                    return 'ok'

            def transport(request):
                requests.append(request)
                return respond(request)

            with httpx.Client(transport=httpx.MockTransport(transport)) as http:
                def discovery(config):
                    queried.append(config)
                    return Client(config, http_client=http)

                previous = Path.cwd()
                output = io.StringIO()
                try:
                    with patch.dict(os.environ, {'HOME': temp, 'LOCAL_KEY': 'local-secret', 'CLOUD_KEY': 'cloud-secret'}), \
                            patch.object(cli, 'Agent', FakeAgent), patch.object(cli, 'Client', side_effect=discovery), \
                            patch.object(cli.sys, 'argv', ['ta', '--no-session', '--no-agent', '--config', str(config), '--directory', temp]), \
                            patch.object(cli.sys, 'stdin', io.StringIO()), patch.object(cli.sys, 'stdout', output), \
                            patch.object(cli, '_readline', None), patch('builtins.input', side_effect=inputs):
                        cli.main()
                finally:
                    os.chdir(previous)
            self.assertEqual(config.read_bytes(), before)
            return output.getvalue(), agents, requests, queried

    def test_explicit_profiles_current_profile_and_refresh_are_lazy_and_do_not_switch(self):
        def respond(request):
            model = 'local-model' if request.url.host == '127.0.0.1' else 'remote-model'
            return httpx.Response(200, json={'data': [{'id': model}]})

        output, agents, requests, queried = self.run_cli(
            ['/api', '/api models cloud', '/api models', '/api models cloud', '/exit'], respond)
        self.assertEqual([config.api_profile for config in queried], ['cloud', 'local', 'cloud'])
        self.assertEqual([request.headers['authorization'] for request in requests],
                         ['Bearer cloud-secret', 'Bearer local-secret', 'Bearer cloud-secret'])
        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0].prompts, [])
        self.assertEqual(agents[0].config.model, 'local-model')
        self.assertIn('remote-model', output)
        self.assertIn('* local-model', output)
        self.assertIn('unlisted-model 不在当前返回列表中', output)
        self.assertNotIn('cloud-secret', output)
        self.assertNotIn('local-secret', output)

    def test_errors_preserve_repl_and_target_profile_credentials_are_redacted(self):
        def respond(request):
            return httpx.Response(403, json={'error': {'message': 'denied cloud-secret'}})

        output, agents, requests, queried = self.run_cli(
            ['/api models cloud', '/api models missing', 'ordinary task', '/exit'], respond)
        self.assertIn('HTTP 403', output)
        self.assertIn('[REDACTED]', output)
        self.assertNotIn('cloud-secret', output)
        self.assertEqual(len(requests), 1)
        self.assertEqual(agents[0].prompts, ['ordinary task'])

    def test_english_catalog_and_help_are_translated(self):
        from termux_agent.cli import HELP
        output, _, _, _ = self.run_cli(['/api models cloud', '/exit'],
            lambda request: httpx.Response(200, json={'data': []}), language='en')
        self.assertIn('0 available models', output)
        self.assertIn('absent from the current catalog', output)
        self.assertIn('Fetch available models from an API', help_text(HELP, 'en'))

    def test_completion_lists_the_query_and_profile_names_without_fetching(self):
        calls = []
        def profiles():
            calls.append('profiles')
            return ['magpie-agentrouter', 'cloud']
        completer = Completer(None, profiles, lambda: [], lambda: [], lambda: [], lambda: Path('.'))
        self.assertEqual(completer.matches('/api mo', 5, 'mo'), ['models'])
        self.assertEqual(completer.matches('/api models mag', 12, 'mag'), ['magpie-agentrouter'])
        self.assertEqual(calls, ['profiles', 'profiles'])
