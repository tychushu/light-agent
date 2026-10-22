"""Responses conversion, stateless tool history, usage and SSE failure boundaries."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from termux_agent.agent import Agent
from termux_agent.api_profiles import ProfileStore
from termux_agent.client import Client, ClientError, ContextLengthError
from termux_agent.config import Config
from termux_agent.sessions import SessionStore, snapshot, validate_messages


def text_item(text):
    return {'type': 'message', 'role': 'assistant', 'id': 'msg_1', 'status': 'completed',
            'content': [{'type': 'output_text', 'text': text, 'annotations': []}]}


def sse(events):
    return ''.join('event: '+e.get('type', 'unknown')+'\ndata: '+json.dumps(e)+'\n\n' for e in events)


class ResponsesTests(unittest.TestCase):
    def test_config_and_profile_persistence_default_chat_and_optional_responses(self):
        self.assertEqual(Config().wire_api, 'chat_completions')
        with tempfile.TemporaryDirectory() as td:
            path = Path(td)/'config.toml'
            path.write_text('wire_api="responses"\n')
            store = ProfileStore(path)
            store.add('new', 'https://example.com/v1', 'test', wire_api='responses')
            self.assertEqual(Config.load(path, 'new').wire_api, 'responses')
            self.assertEqual(store.list()[0]['wire_api'], 'responses')
            self.assertEqual(Config.load(path).wire_api, 'responses')
            for value in ('anthropic', '', True, 1):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    Config(wire_api=value).validate()

    def test_messages_and_tools_convert_without_mutating_originals(self):
        messages = [{'role': 'system', 'content': '规则'},
                    {'role': 'user', 'content': [{'type': 'text', 'text': 'hello'}]},
                    {'role': 'assistant', 'content': 'reading', 'tool_calls': [{'id': 'call_1',
                     'function': {'name': 'read_file', 'arguments': {'path': '/tmp/a'}}}]},
                    {'role': 'tool', 'tool_call_id': 'call_1', 'content': '{"ok":true}'}]
        tools = [{'type': 'function', 'function': {'name': 'read_file', 'parameters': {'type': 'object'}}}]
        before = copy.deepcopy((messages, tools))
        with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as http:
            client = Client(Config(wire_api='responses'), http_client=http)
            url, body = client._prepare_responses_request(messages, tools)
        self.assertTrue(url.endswith('/v1/responses'))
        self.assertEqual(body['input'][0]['content'][0]['type'], 'input_text')
        self.assertEqual(body['input'][1]['content'][0]['text'], 'hello')
        self.assertEqual(body['input'][2]['content'][0]['type'], 'output_text')
        self.assertEqual(body['input'][3]['type'], 'function_call')
        self.assertEqual(json.loads(body['input'][3]['arguments']), {'path': '/tmp/a'})
        self.assertEqual(body['input'][4]['type'], 'function_call_output')
        self.assertEqual(body['input'][4]['call_id'], 'call_1')
        self.assertFalse(body['tools'][0]['strict'])
        self.assertFalse(body['store'])
        self.assertEqual((messages, tools), before)

    def test_json_usage_maps_to_existing_stats_and_reasoning_tokens(self):
        result = {'status': 'completed', 'output': [text_item('ok')],
                  'usage': {'input_tokens': 5, 'output_tokens': 8, 'total_tokens': 13,
                            'output_tokens_details': {'reasoning_tokens': 6}}}
        with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=result))) as http:
            client = Client(Config(wire_api='responses', stream=False), http_client=http)
            agent = Agent(client.config, client=client)
            self.assertEqual(agent.run('hello'), 'ok')
            usage = agent.stats()['usage_totals']
            self.assertEqual((usage['prompt_tokens'], usage['completion_tokens'], usage['reasoning_tokens']), (5, 8, 6))

    def test_tool_loop_preserves_reasoning_items_through_checkpoint_and_next_request(self):
        reasoning = {'type': 'reasoning', 'id': 'rs_1', 'summary': [], 'encrypted_content': 'OPAQUE_SIGNATURE'}
        call = {'type': 'function_call', 'id': 'fc_1', 'call_id': 'call_1', 'name': 'read_file',
                'arguments': '{"path":"/tmp/example"}', 'status': 'completed'}
        seen = []
        def respond(request):
            seen.append(json.loads(request.content))
            output = [reasoning, call] if len(seen) == 1 else [text_item('done')]
            return httpx.Response(200, json={'status': 'completed', 'output': output})
        with httpx.Client(transport=httpx.MockTransport(respond)) as http:
            cfg = Config(wire_api='responses', stream=False, approval_policy='never')
            agent = Agent(cfg, client=Client(cfg, http_client=http))
            with patch('termux_agent.agent.execute', return_value={'content': 'file'}) as execute:
                self.assertEqual(agent.run('read it'), 'done')
            self.assertEqual(execute.call_count, 1)
            expected_reasoning = {k: v for k, v in reasoning.items() if k != 'id'}
            expected_call = {k: v for k, v in call.items() if k != 'id'}
            self.assertEqual(seen[1]['input'][2:4], [expected_reasoning, expected_call])
            self.assertEqual(seen[1]['input'][4]['call_id'], 'call_1')
            payload = snapshot(agent, {}, checkpoint=True)
            validate_messages(payload['messages'])
            with tempfile.TemporaryDirectory() as td:
                store = SessionStore(td)
                store.save('abcdef123456', 0, payload)
                self.assertEqual(store.load('abcdef123456')[1]['messages'][2]['_responses_output'], [reasoning, call])
                store.close()

    def test_chat_requests_do_not_send_responses_private_metadata(self):
        seen = []
        def respond(request):
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={'choices': [{'message': {'content': 'ok'}}]})
        with httpx.Client(transport=httpx.MockTransport(respond)) as http:
            client = Client(Config(stream=False), http_client=http)
            client.chat([{'role': 'assistant', 'content': 'hello', '_responses_output': [text_item('hello')]}], [])
        self.assertNotIn('_responses_output', seen[0]['messages'][0])

    def test_sse_multiple_calls_final_output_and_reasoning_summary_are_not_duplicated(self):
        r = {'type': 'reasoning', 'summary': [{'type': 'summary_text', 'text': 'think'}], 'encrypted_content': 'opaque'}
        call1 = {'type': 'function_call', 'call_id': 'c1', 'name': 'read_file', 'arguments': '{}'}
        call2 = {'type': 'function_call', 'call_id': 'c2', 'name': 'list_files', 'arguments': '{"path":"."}'}
        events = [{'type': 'response.reasoning_summary_text.delta', 'delta': 'think'},
                  {'type': 'response.output_item.added', 'output_index': 1, 'item': dict(call1, arguments='')},
                  {'type': 'response.function_call_arguments.delta', 'output_index': 1, 'delta': '{'},
                  {'type': 'response.function_call_arguments.delta', 'output_index': 1, 'delta': '}'},
                  {'type': 'response.output_item.done', 'output_index': 1, 'item': call1},
                  {'type': 'response.output_item.done', 'output_index': 2, 'item': call2},
                  {'type': 'response.output_text.delta', 'delta': 'Hi'},
                  {'type': 'response.completed', 'response': {'status': 'completed',
                    'output': [r, call1, call2, text_item('Hi')], 'usage': {'input_tokens': 2, 'output_tokens': 3}}}]
        with httpx.Client(transport=httpx.MockTransport(lambda q: httpx.Response(200, text=sse(events)))) as http:
            client = Client(Config(wire_api='responses'), http_client=http)
            streamed = []
            client.on_stream = lambda k, v: streamed.append((k, v))
            answer = client.chat([], [])
        self.assertEqual(answer['content'], 'Hi')
        self.assertEqual(answer['reasoning_content'], 'think')
        self.assertEqual([c['id'] for c in answer['tool_calls']], ['c1', 'c2'])
        self.assertEqual(streamed, [('reasoning', 'think'), ('content', 'Hi')])
        self.assertEqual(client.last_usage['prompt_tokens'], 2)

    def test_final_only_sse_response_returns_text(self):
        events = [{'type': 'response.completed', 'response': {'status': 'completed', 'output': [text_item('done')]}}]
        with httpx.Client(transport=httpx.MockTransport(lambda q: httpx.Response(200, text=sse(events)))) as http:
            self.assertEqual(Client(Config(wire_api='responses'), http_client=http).chat([], [])['content'], 'done')

    def test_failed_incomplete_and_truncated_streams_never_retry_or_execute_tools(self):
        call = {'type': 'function_call', 'name': 'shell', 'call_id': 'c', 'arguments': '{"command":"must not run"}'}
        prefix = [{'type': 'response.output_item.added', 'item': call, 'output_index': 0}]
        tails = [[], [{'type': 'error', 'code': 'PRIVATE_KEY', 'message': 'PRIVATE_BODY'}],
                 [{'type': 'response.failed', 'response': {'status': 'failed', 'error': {'message': 'PRIVATE_BODY'}}}],
                 [{'type': 'response.incomplete', 'response': {'status': 'incomplete', 'output': [call],
                   'incomplete_details': {'reason': 'max_output_tokens'}, 'usage': {'output_tokens': 10}}}]]
        for tail in tails:
            with self.subTest(tail=tail):
                requests = []
                def respond(request):
                    requests.append(request)
                    return httpx.Response(200, text=sse(prefix + tail))
                with httpx.Client(transport=httpx.MockTransport(respond)) as http:
                    cfg = Config(wire_api='responses', approval_policy='never')
                    agent = Agent(cfg, client=Client(cfg, http_client=http))
                    with patch('termux_agent.agent.execute') as execute:
                        error = agent.run('task')
                    execute.assert_not_called()
                    self.assertIn('LLM 请求失败', error)
                    self.assertNotIn('PRIVATE', error)
                self.assertEqual(len(requests), 1)

    def test_json_incomplete_tool_calls_are_rejected_and_refusals_are_preserved(self):
        for status in ('in_progress', 'incomplete', 'failed'):
            with httpx.Client(transport=httpx.MockTransport(lambda q: httpx.Response(200, json={'status': status, 'output': []}))) as http:
                with self.assertRaises(ClientError):
                    Client(Config(wire_api='responses', stream=False), http_client=http).chat([], [])
        refusal = {'type': 'message', 'content': [{'type': 'refusal', 'refusal': 'Cannot help'}]}
        with httpx.Client(transport=httpx.MockTransport(lambda q: httpx.Response(200, json={'output': [refusal]}))) as http:
            answer = Client(Config(wire_api='responses', stream=False), http_client=http).chat([], [])
        self.assertEqual(answer['refusal'], 'Cannot help')

    def test_stream_context_error_is_typed_and_silent_write_debug_stays_hidden(self):
        events = [{'type': 'error', 'code': 'context_length_exceeded', 'message': 'PRIVATE'}]
        with httpx.Client(transport=httpx.MockTransport(lambda q: httpx.Response(200, text=sse(events)))) as http:
            with self.assertRaises(ContextLengthError):
                Client(Config(wire_api='responses'), http_client=http).chat([], [])
            client = Client(Config(wire_api='responses'), http_client=http)
            client.last_request = {'input': [{'type': 'function_call', 'name': 'write_file',
                                    'arguments': '{"path":"note", "content":"PRIVATE_BODY"}'}]}
            agent = Agent(client.config, client=client)
            self.assertNotIn('PRIVATE_BODY', json.dumps(agent.debug_context()))
