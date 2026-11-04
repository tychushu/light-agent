"""SSE framing, receive limits and projection regressions."""
import copy
from contextlib import closing
import io
import json
import unittest

import httpx

from termux_agent.agent import Agent
from termux_agent.client import Client, ResponseLimitError
from termux_agent.config import Config
from termux_agent.model_input import project_tool_messages
from termux_agent.streaming import iter_sse_data, limited_draft
from termux_agent.terminal_ui import Columns


class Chunks(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    def __iter__(self):
        yield from self.chunks


def event(value):
    return ('data: ' + json.dumps(value, ensure_ascii=False) + '\n\n').encode()


class OptimizationStreamingTests(unittest.TestCase):
    def test_shared_sse_frames_split_utf8_crlf_multiline_and_final_record(self):
        wire = ': comment\r\ndata: {"text":\r\ndata: "中文"}\r\n\r\ndata: [DONE]'.encode()
        response = httpx.Response(200, stream=Chunks([wire[i:i + 1] for i in range(len(wire))]))
        with closing(response):
            values = list(iter_sse_data(response, 4096))
        self.assertEqual(json.loads(values[0]), {'text': '中文'})
        self.assertEqual(values[1], '[DONE]')

    def test_stream_yields_first_event_without_waiting_for_later_network_chunks(self):
        consumed = []
        class Response:
            def iter_bytes(self):
                consumed.append('first'); yield b'data: first\n\n'
                consumed.append('later'); yield b'data: later\n\n'
        values = iter_sse_data(Response(), 1024)
        self.assertEqual(next(values), 'first')
        self.assertEqual(consumed, ['first'])
        values.close()

    def test_long_unterminated_line_hits_cap_before_json_decoding(self):
        response = httpx.Response(200, stream=Chunks([b'data: ', b'x' * 2048]))
        with closing(response), self.assertRaises(ResponseLimitError):
            list(iter_sse_data(response, 1024))

    def test_chat_fragment_lists_preserve_exact_unicode_tool_arguments(self):
        arguments = json.dumps({'path': 'note.txt', 'content': '正文' * 2000}, ensure_ascii=False)
        chunks = [event({'choices': [{'delta': {'tool_calls': [
            {'index': 0, 'id': 'call_1', 'function': {'name': 'write_file', 'arguments': ''}}]}}]})]
        chunks.extend(event({'choices': [{'delta': {'tool_calls': [
            {'index': 0, 'function': {'arguments': piece}}]}}]}) for piece in arguments)
        chunks.append(event({'choices': [{'delta': {}, 'finish_reason': 'tool_calls'}]}))
        chunks.append(b'data: [DONE]\n\n')
        with httpx.Client(transport=httpx.MockTransport(lambda q: httpx.Response(200, stream=Chunks(chunks)))) as http:
            answer = Client(Config(), http_client=http).chat([], [])
        self.assertEqual(answer['tool_calls'][0]['function']['arguments'], arguments)

    def test_responses_fragment_lists_and_authoritative_done_do_not_duplicate_arguments(self):
        arguments = json.dumps({'path': 'note.txt', 'content': '正文' * 2000}, ensure_ascii=False)
        for done in (False, True):
            item = {'type': 'function_call', 'call_id': 'call_1', 'name': 'write_file', 'arguments': ''}
            chunks = [event({'type': 'response.output_item.added', 'output_index': 0, 'item': item})]
            chunks.extend(event({'type': 'response.function_call_arguments.delta', 'output_index': 0,
                                 'delta': piece}) for piece in arguments)
            if done:
                chunks.append(event({'type': 'response.function_call_arguments.done', 'output_index': 0,
                                     'arguments': arguments}))
                chunks.append(event({'type': 'response.output_item.done', 'output_index': 0,
                                     'item': {**item, 'arguments': arguments}}))
            chunks.append(event({'type': 'response.completed', 'response': {'status': 'completed'}}))
            with self.subTest(done=done), httpx.Client(transport=httpx.MockTransport(
                    lambda q: httpx.Response(200, stream=Chunks(chunks)))) as http:
                answer = Client(Config(wire_api='responses'), http_client=http).chat([], [])
            self.assertEqual(answer['tool_calls'][0]['function']['arguments'], arguments)

    def test_oversized_stream_is_not_retried_and_no_partial_tool_is_executed(self):
        for wire_api in ('chat_completions', 'responses'):
            if wire_api == 'responses':
                chunks = [event({'type': 'response.output_item.added', 'output_index': 0, 'item': {
                    'type': 'function_call', 'call_id': 'x', 'name': 'write_file', 'arguments': '{'}})]
            else:
                chunks = [event({'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'x',
                    'function': {'name': 'write_file', 'arguments': '{'}}]}}]})]
            chunks.append(b':' + b'x' * 2048)
            sent = []
            def respond(request):
                sent.append(request)
                return httpx.Response(200, stream=Chunks(chunks))
            with self.subTest(wire_api=wire_api), httpx.Client(transport=httpx.MockTransport(respond)) as http:
                a = Agent(Config(wire_api=wire_api, max_response_bytes=1024, approval_policy='never', token_count_mode='off'),
                          client=Client(Config(wire_api=wire_api, max_response_bytes=1024), http_client=http), base_system='rules')
                from unittest.mock import patch
                with patch('termux_agent.agent.execute') as execute:
                    a.run('write')
                execute.assert_not_called()
                self.assertEqual(a.last_run['stop_reason'], 'response_limit')
                self.assertEqual(len(sent), 1)
                self.assertNotIn('tool_calls', a.incomplete_draft)
                self.assertTrue(a.incomplete_draft['_incomplete_tool_calls'])
                a.close()

    def test_oversized_json_accepted_response_is_not_retried(self):
        sent = []
        def respond(request):
            sent.append(request)
            return httpx.Response(200, json={'choices': [{'message': {'content': 'x' * 2048}}]})
        with httpx.Client(transport=httpx.MockTransport(respond)) as http:
            client = Client(Config(stream=False, max_response_bytes=1024), http_client=http)
            with self.assertRaises(ResponseLimitError):
                client.chat([], [])
        self.assertEqual(len(sent), 1)

    def test_receive_limit_draft_is_bounded_without_executable_protocol_items(self):
        calls = [{'id': str(i), 'function': {'name': 'write_file', 'arguments': 'x' * 100000}}
                 for i in range(40)]
        draft = limited_draft('a' * 100000, 'b' * 100000, calls)
        self.assertTrue(draft['truncated'])
        self.assertEqual(len(draft['content']), 64000)
        self.assertEqual(len(draft['reasoning_content']), 64000)
        self.assertEqual(len(draft['_incomplete_tool_calls']), 32)
        self.assertEqual(sum(len(c['function']['arguments']) for c in draft['_incomplete_tool_calls']), 64000)
        self.assertNotIn('tool_calls', draft)
        self.assertNotIn('_responses_output', draft)

    def test_receive_cap_can_be_set_per_profile_and_rejects_bad_types(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'config.toml'
            path.write_text('max_response_bytes=2048\n[apis.small]\nbase_url="https://example.test/v1"\n'
                            'model="test"\nmax_response_bytes=1024\n')
            self.assertEqual(Config.load(path).max_response_bytes, 2048)
            self.assertEqual(Config.load(path, 'small').max_response_bytes, 1024)
        for value in (True, 1023, 64 * 1024 * 1024 + 1, '2048', 2048.0):
            with self.assertRaises(ValueError):
                Config(max_response_bytes=value).validate()

    def test_model_projection_preserves_outputs_statuses_and_recovery_reference(self):
        receipt = {'stdout': 'output', 'stderr': 'failure detail', 'exit_code': 7,
                   'execution_status': 'executed', 'outcome_status': 'failed',
                   'output_compression': {'status': 'compressed', 'applied': True, 'before_bytes': 1000,
                       'after_bytes': 500, 'raw_digest': 'a' * 64, 'raw_available': True,
                       'raw_capture_complete': True, 'raw_artifact': {'id': 'raw-1', 'path': '/private/raw-1.json', 'bytes': 900}}}
        messages = [{'role': 'tool', 'tool_call_id': 'x', 'content': json.dumps(receipt)}]
        original = copy.deepcopy(messages)
        projected = project_tool_messages(messages)
        value = json.loads(projected[0]['content'])
        self.assertEqual(messages, original)
        for field in ('stdout', 'stderr', 'exit_code', 'execution_status', 'outcome_status'):
            self.assertEqual(value[field], receipt[field])
        self.assertEqual(value['output_compression']['raw_artifact']['path'], '/private/raw-1.json')
        self.assertNotIn('raw_digest', value['output_compression'])
        self.assertLess(len(projected[0]['content']), len(messages[0]['content']))

    def test_columns_batch_writes_and_keep_layout_with_chunked_input(self):
        class Output(io.StringIO):
            writes = 0
            def write(self, text):
                self.writes += 1
                return super().write(text)
        text = '中文 ' * 100 + '\tend\nsecond line\x1b'
        out = Output(); columns = Columns(out, width=lambda: 80)
        columns.feed('Agent', text, stamp='12:00')
        self.assertEqual(out.writes, 1)
        chunked = io.StringIO(); other = Columns(chunked, width=lambda: 80)
        for char in text:
            other.feed('Agent', char, stamp='12:00')
        self.assertEqual(out.getvalue(), chunked.getvalue())
