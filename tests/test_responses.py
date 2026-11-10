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
        tails = [[], [{'type': 'error', 'code': 'RATE_LIMIT', 'message': 'rate limit hit'}],
                 [{'type': 'response.failed', 'response': {'status': 'failed', 'error': {'message': 'gateway error'}}}],
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
                self.assertEqual(len(requests), 1)

    def test_stream_error_passes_through_upstream_message(self):
        # An output-free stream with a rate limit is now a bounded empty-stream
        # retry; keep one attempt so this stays a pure diagnostic passthrough test.
        events = [{'type': 'error', 'code': 'rate_limit_exceeded', 'message': 'upstream rate limit hit'}]
        with httpx.Client(transport=httpx.MockTransport(lambda q: httpx.Response(200, text=sse(events)))) as http:
            with self.assertRaises(ClientError) as caught:
                Client(Config(wire_api='responses', retry_max_attempts=1), http_client=http).chat([], [])
        self.assertIn('upstream rate limit hit', str(caught.exception))
        self.assertIn('rate_limit_exceeded', str(caught.exception))

    def test_stream_error_redaction_and_control_escaping_happen_before_bounding(self):
        secret = 'SSE_BOUNDARY_KEY_0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'
        message = 'rate limited\x1b[31m\n' + 'A' * 1950 + secret + 'Z' * 3000
        events = [{'type': 'error', 'error': {}, 'code': 'rate_limit_exceeded', 'message': message}]
        with httpx.Client(transport=httpx.MockTransport(lambda q: httpx.Response(200, text=sse(events)))) as http:
            with self.assertRaises(ClientError) as caught:
                Client(Config(wire_api='responses', api_key=secret), http_client=http).chat([], [])
        diagnostic = str(caught.exception)
        self.assertIn('rate_limit_exceeded', diagnostic)
        self.assertIn('[REDACTED]', diagnostic)
        self.assertIn('\\x1b', diagnostic)
        self.assertNotIn('\x1b', diagnostic)
        self.assertNotIn('\n', diagnostic)
        self.assertNotIn('SSE_BOUNDARY_KEY', diagnostic)
        self.assertLessEqual(len(diagnostic), 2100)

    def test_stream_nested_and_top_level_fields_include_numeric_codes_and_string_errors(self):
        cases = [({'type': 'error', 'error': {'message': 'nested message'}, 'code': 'root_code'}, 'root_code', 'nested message'),
                 ({'type': 'error', 'error': {}, 'code': 0, 'msg': 'root message'}, '0', 'root message'),
                 ({'type': 'error', 'error': {'code': {}, 'message': False}, 'code': 403, 'msg': 'denied'}, '403', 'denied'),
                 ({'type': 'response.failed', 'response': {'error': 'plain gateway error', 'code': 'gw'}}, 'gw', 'plain gateway error')]
        for event, code, message in cases:
            with self.subTest(event=event):
                with httpx.Client(transport=httpx.MockTransport(lambda q: httpx.Response(200, text=sse([event])))) as http:
                    with self.assertRaises(ClientError) as caught:
                        Client(Config(wire_api='responses'), http_client=http).chat([], [])
                self.assertIn(f'（{code}: {message}）', str(caught.exception))

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

    def _failed_with_output(self, events):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(200, text=sse(events))

        with httpx.Client(transport=httpx.MockTransport(respond)) as http:
            client = Client(Config(wire_api='responses'), http_client=http)
            with self.assertRaises(ClientError) as caught:
                client.chat([], [])
        self.assertEqual(len(requests), 1)  # a partial stream is never replayed
        return caught.exception.assistant_message

    def test_failed_frame_output_merges_with_streamed_deltas_without_duplicates(self):
        # The failure frame's output repeats the same text and call_id already
        # streamed as deltas; align/merge by index and call_id, do not duplicate.
        events = [
            {'type': 'response.output_text.delta', 'output_index': 0, 'delta': 'partial answer'},
            {'type': 'response.output_item.added', 'output_index': 1, 'item': {
                'type': 'function_call', 'call_id': 'call_1', 'name': 'read_file', 'arguments': ''}},
            {'type': 'response.function_call_arguments.delta', 'output_index': 1, 'delta': '{"path":'},
            {'type': 'response.failed', 'response': {'status': 'failed',
                'error': {'code': 'rate_limit_exceeded'},
                'output': [
                    # Reordered: the tool must align by call_id, not by position.
                    {'type': 'function_call', 'call_id': 'call_1', 'name': 'read_file',
                     'arguments': '{"path":"/tmp/a"}'},
                    {'type': 'message', 'id': 'msg_1', 'role': 'assistant',
                     'content': [{'type': 'output_text', 'text': 'partial answer'}]},
                ]}},
        ]
        draft = self._failed_with_output(events)
        self.assertEqual(draft['content'], 'partial answer')       # no duplicated text
        self.assertNotIn('tool_calls', draft)                       # draft stays inert
        calls = draft.get('_incomplete_tool_calls', [])
        self.assertEqual(len(calls), 1)                            # no duplicated tool
        self.assertEqual(calls[0]['id'], 'call_1')
        self.assertEqual(calls[0]['function']['arguments'], '{"path":"/tmp/a"}')

    def test_failed_frame_output_keeps_non_overlapping_delta_fragments(self):
        # A snapshot text extending the delta prefix is merged, and tool argument
        # fragments longer than the snapshot are preserved rather than halved.
        events = [
            {'type': 'response.output_text.delta', 'delta': 'partial'},
            {'type': 'response.output_item.added', 'output_index': 0, 'item': {
                'type': 'function_call', 'call_id': 'c', 'name': 'write_file', 'arguments': ''}},
            {'type': 'response.function_call_arguments.delta', 'output_index': 0,
             'delta': '{"path":"/tmp/a","extra":true}'},
            {'type': 'response.failed', 'response': {'status': 'failed',
                'error': {'code': 'rate_limit_exceeded'},
                'output': [
                    {'type': 'message', 'content': [{'type': 'output_text', 'text': 'partial answer'}]},
                    {'type': 'function_call', 'call_id': 'c', 'name': 'write_file',
                     'arguments': '{"path":"/tmp/a"}'},
                ]}},
        ]
        draft = self._failed_with_output(events)
        self.assertEqual(draft['content'], 'partial answer')
        calls = draft.get('_incomplete_tool_calls', [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['function']['arguments'], '{"path":"/tmp/a","extra":true}')

    def test_failed_frame_output_merges_indexed_messages_without_suffix_scan(self):
        # A at index 0 (id msg_1, delta 'Partial'), a tool at index 1, and B at
        # index 2 (id msg_2, delta 'Uncovered fragment'). The failure snapshot
        # only repeats A's text and the tool's complete arguments. Merging must
        # stay per item: A becomes 'Partial answer', B survives, order preserved.
        events = [
            {'type': 'response.output_item.added', 'output_index': 0,
             'item': {'type': 'message', 'id': 'msg_1', 'role': 'assistant', 'content': []}},
            {'type': 'response.output_text.delta', 'output_index': 0, 'delta': 'Partial'},
            {'type': 'response.output_item.added', 'output_index': 1,
             'item': {'type': 'function_call', 'call_id': 'c1', 'name': 'write_file', 'arguments': ''}},
            {'type': 'response.function_call_arguments.delta', 'output_index': 1, 'delta': '{"path":'},
            {'type': 'response.output_item.added', 'output_index': 2,
             'item': {'type': 'message', 'id': 'msg_2', 'role': 'assistant', 'content': []}},
            {'type': 'response.output_text.delta', 'output_index': 2, 'delta': 'Uncovered fragment'},
            {'type': 'response.failed', 'response': {'status': 'failed',
                'error': {'code': 'rate_limit_exceeded'},
                'output': [
                    {'type': 'message', 'id': 'msg_1', 'role': 'assistant',
                     'content': [{'type': 'output_text', 'text': 'Partial answer'}]},
                    {'type': 'function_call', 'call_id': 'c1', 'name': 'write_file',
                     'arguments': '{"path":"/tmp/a"}'},
                ]}},
        ]
        draft = self._failed_with_output(events)
        self.assertEqual(draft['content'], 'Partial answerUncovered fragment')
        self.assertNotIn('tool_calls', draft)                 # inert draft
        calls = draft.get('_incomplete_tool_calls', [])
        self.assertEqual(len(calls), 1)                      # each call_id once
        self.assertEqual(calls[0]['id'], 'c1')
        self.assertEqual(calls[0]['function']['arguments'], '{"path":"/tmp/a"}')

    def test_failed_frame_output_merges_reasoning_fragments_per_item(self):
        events = [
            {'type': 'response.output_item.added', 'output_index': 0,
             'item': {'type': 'reasoning', 'id': 'rs_1', 'summary': []}},
            {'type': 'response.reasoning_text.delta', 'output_index': 0, 'delta': 'Thou'},
            {'type': 'response.output_text.delta', 'output_index': 1, 'delta': 'Body'},
            {'type': 'response.output_item.added', 'output_index': 2,
             'item': {'type': 'reasoning', 'id': 'rs_2', 'summary': []}},
            {'type': 'response.reasoning_text.delta', 'output_index': 2, 'delta': 'Uncovered thought'},
            {'type': 'response.failed', 'response': {'status': 'failed',
                'error': {'code': 'rate_limit_exceeded'},
                'output': [
                    {'type': 'reasoning', 'id': 'rs_1',
                     'content': [{'type': 'reasoning_text', 'text': 'Thought'}]},
                ]}},
        ]
        draft = self._failed_with_output(events)
        self.assertEqual(draft['reasoning_content'], 'ThoughtUncovered thought')
        self.assertEqual(draft['content'], 'Body')

    def test_failed_frame_output_without_item_metadata_stays_compatible(self):
        # Simplified gateway: bare deltas with no ids/output_index at all.
        events = [
            {'type': 'response.output_text.delta', 'delta': 'plain'},
            {'type': 'response.failed', 'response': {'status': 'failed',
                'error': {'code': 'rate_limit_exceeded'},
                'output': [
                    {'type': 'message', 'role': 'assistant',
                     'content': [{'type': 'output_text', 'text': 'plain text'}]},
                ]}},
        ]
        draft = self._failed_with_output(events)
        self.assertEqual(draft['content'], 'plain text')   # extended once, no duplication
        self.assertEqual(draft.get('_incomplete_tool_calls', []), [])

    def test_delta_item_id_aligns_same_prefix_items_when_added_is_omitted(self):
        # Gateway omits output_item.added: two deltas share the 'same' prefix
        # but carry distinct item_id. The failure snapshot only names id=b.
        events = [
            {'type': 'response.output_text.delta', 'output_index': 0,
             'item_id': 'a', 'delta': 'same'},
            {'type': 'response.output_text.delta', 'output_index': 2,
             'item_id': 'b', 'delta': 'same'},
            {'type': 'response.failed', 'response': {'status': 'failed',
                'error': {'code': 'rate_limit_exceeded'},
                'output': [
                    {'type': 'message', 'id': 'b', 'role': 'assistant',
                     'content': [{'type': 'output_text', 'text': 'same completed'}]},
                ]}},
        ]
        draft = self._failed_with_output(events)
        self.assertEqual(draft['content'], 'samesame completed')
        self.assertNotIn('tool_calls', draft)
        self.assertEqual(draft.get('_incomplete_tool_calls', []), [])

    def test_new_snapshot_id_never_claims_an_index_with_a_known_different_id(self):
        # index 0 is already bound to id=a; a failure-frame item with a NEW id=b
        # and the same text prefix must not overwrite a's draft. Both survive.
        events = [
            {'type': 'response.output_text.delta', 'output_index': 0,
             'item_id': 'a', 'delta': 'same'},
            {'type': 'response.failed', 'response': {'status': 'failed',
                'error': {'code': 'rate_limit_exceeded'},
                'output': [
                    {'type': 'message', 'id': 'b', 'role': 'assistant',
                     'content': [{'type': 'output_text', 'text': 'same completed'}]},
                ]}},
        ]
        draft = self._failed_with_output(events)
        self.assertEqual(draft['content'], 'samesame completed')
        self.assertNotIn('tool_calls', draft)
        self.assertEqual(draft.get('_incomplete_tool_calls', []), [])
