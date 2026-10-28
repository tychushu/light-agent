"""Focused configuration and canonical wire-history validation."""
import json
import unittest
from unittest.mock import patch

import httpx

from termux_agent.client import Client
from termux_agent.config import Config
from termux_agent.token_budget import TokenCounter


class ConfigWireValidationTests(unittest.TestCase):
    def test_wire_and_token_settings_are_validated(self):
        invalid = (
            {'wire_api': 'json'},
            {'token_count_mode': 'exact'},
            {'chat_max_tokens_field': 'output_tokens'},
            {'context_window_tokens': 1024, 'output_reserve_tokens': 1024},
            {'max_output_tokens': 256, 'output_reserve_tokens': 128},
            {'reasoning_effort': 'extreme'},
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                Config(**values).validate()

    def test_responses_history_preserves_call_id_but_strips_provider_id_and_drafts(self):
        captured = {}

        def respond(request):
            captured['body'] = json.loads(request.content)
            return httpx.Response(200, json={'status': 'completed', 'output': [
                {'type': 'message', 'role': 'assistant',
                 'content': [{'type': 'output_text', 'text': 'ok'}]},
            ]})

        config = Config(base_url='http://127.0.0.1:9/v1', model='wire-test',
                        wire_api='responses', stream=False)
        http = httpx.Client(transport=httpx.MockTransport(respond))
        client = Client(config, http_client=http)
        messages = [
            {'role': 'user', 'content': 'run'},
            {'role': 'assistant', 'content': 'complete',
             'tool_calls': [{'id': 'call-1', 'type': 'function',
                             'function': {'name': 'shell', 'arguments': '{}'}}],
             '_responses_output': [
                 {'type': 'reasoning', 'id': 'provider-private-id', 'encrypted_content': 'opaque'},
                 {'type': 'function_call', 'id': 'provider-call-id', 'call_id': 'call-1',
                  'name': 'shell', 'arguments': '{}'},
             ],
             '_incomplete_tool_calls': [{'id': 'partial', 'function': {'name': 'write_file'}}],
             '_incomplete_output': {'private': True}},
            {'role': 'tool', 'tool_call_id': 'call-1', 'content': '{}'},
        ]
        client.chat(messages, [])
        wire = json.dumps(captured['body'], ensure_ascii=False)
        self.assertNotIn('provider-private-id', wire)
        self.assertNotIn('provider-call-id', wire)
        self.assertNotIn('_incomplete', wire)
        self.assertNotIn('partial', wire)
        self.assertIn('"call_id":"call-1"', wire.replace(' ', ''))
        http.close()

    def test_chat_history_removes_private_draft_fields(self):
        captured = {}

        def respond(request):
            captured['body'] = json.loads(request.content)
            return httpx.Response(200, json={'choices': [
                {'message': {'content': 'ok'}, 'finish_reason': 'stop'}]})

        config = Config(base_url='http://127.0.0.1:9/v1', model='wire-test',
                        wire_api='chat_completions', stream=False)
        http = httpx.Client(transport=httpx.MockTransport(respond))
        client = Client(config, http_client=http)
        client.chat([{'role': 'assistant', 'content': 'draft',
                      '_responses_output': [{'type': 'message'}],
                      '_incomplete_output': {'private': True},
                      '_incomplete_tool_calls': [{'id': 'partial'}]}], [])
        wire = json.dumps(captured['body'], ensure_ascii=False)
        self.assertNotIn('_responses_output', wire)
        self.assertNotIn('_incomplete', wire)
        self.assertNotIn('partial', wire)
        http.close()

    def test_owned_token_counter_client_closes(self):
        class FakeHttp:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        created = []

        def make_client(*args, **kwargs):
            client = FakeHttp()
            created.append(client)
            return client

        counter = TokenCounter(Config())
        with patch('termux_agent.token_budget.httpx.Client', side_effect=make_client):
            counter._client()
        counter.close()
        self.assertEqual(len(created), 1)
        self.assertTrue(created[0].closed)


if __name__ == '__main__':
    unittest.main()
