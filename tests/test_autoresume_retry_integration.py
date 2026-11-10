"""The outer automatic driver must preserve the real Client's retry safety contract."""
from contextlib import contextmanager
import json
import os
import signal
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import httpx
import test_mimo_acceptance as fixtures
from termux_agent.agent import Agent
from termux_agent.client import Client, NetworkError, RateLimitError
from termux_agent.config import Config
from termux_agent.sessions import validate_messages


def frame(value):
    return ('data: ' + json.dumps(value) + '\n\n').encode()


def answer(content='done [TASK_DONE]', calls=None):
    message = {'content': content}
    if calls:
        message['tool_calls'] = calls
    return httpx.Response(200, json={'choices': [{'message': message, 'finish_reason': 'stop'}]})


def responses_text_item(text):
    return {'type': 'message', 'role': 'assistant', 'id': 'msg', 'status': 'completed',
            'content': [{'type': 'output_text', 'text': text, 'annotations': []}]}


def responses_completed(content='done', calls=None):
    output = [{'type': 'function_call', 'call_id': c['id'], 'name': c['function']['name'],
               'arguments': c['function']['arguments'], 'status': 'completed'} for c in (calls or [])]
    if content:
        output.append(responses_text_item(content))
    return frame({'type': 'response.completed', 'response': {'status': 'completed', 'output': output}})


def sse_response(body, headers=None):
    merged = {'Content-Type': 'text/event-stream'}
    if headers:
        merged.update(headers)
    return httpx.Response(200, headers=merged, stream=httpx.ByteStream(body))


def empty_rate_limit_failed():
    return frame({'type': 'response.failed', 'response': {
        'status': 'failed', 'error': {'code': 'rate_limit_exceeded', 'message': 'slow down'}}})


def empty_rate_limit_error():
    return frame({'type': 'error', 'code': 'rate_limit_exceeded', 'message': 'slow down'})


class Clock:
    def __init__(self):
        self.now, self.wall = 100.0, 1000.0
        self.sleeps = []

    def advance(self, seconds):
        self.now += seconds
        self.wall += seconds

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.advance(seconds)


class BrokenStream(httpx.SyncByteStream):
    def __init__(self, body):
        self.body = body

    def __iter__(self):
        yield self.body
        raise httpx.ReadError('accepted body disconnected')


class AutoResumeRetryIntegrationTests(unittest.TestCase):
    @contextmanager
    def build(self, respond, **changes):
        values = dict(auto_continue=True, auto_resume=True, auto_compact=False, approval_policy='never',
                      max_steps=10, max_task_calls=50, max_task_seconds=600, retry_max_attempts=1,
                      retry_wait_budget=20, retry_total_timeout=300, token_count_mode='off', stream=False)
        values.update(changes)
        cfg, clock, sent = Config(**values), Clock(), []
        def handle(request):
            sent.append((clock.now, request.content))
            return respond(request, len(sent), clock)
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {'HOME': home}), \
             patch('termux_agent.agent.time.monotonic', side_effect=lambda: clock.now), \
             patch('termux_agent.agent.time.time', side_effect=lambda: clock.wall), \
             httpx.Client(transport=httpx.MockTransport(handle)) as http:
            client = Client(cfg, http_client=http, sleeper=clock.sleep, clock=lambda: clock.now,
                            wall_clock=lambda: clock.wall, randomizer=lambda: 0)
            agent = Agent(cfg, client=client, base_system='rules')
            agent._sleep = clock.sleep
            try:
                yield agent, clock, sent
            finally:
                agent.close()

    def test_accepted_rate_limit_and_read_error_never_replay_either_wire_api(self):
        for wire in ('chat_completions', 'responses'):
            for failure in ('rate_limit', 'read_error'):
                with self.subTest(wire=wire, failure=failure):
                    if wire == 'responses':
                        body = frame({'type': 'response.output_text.delta', 'delta': 'partial draft'})
                        body += frame({'type': 'response.output_item.added', 'output_index': 0, 'item': {
                            'type': 'function_call', 'call_id': 'w', 'name': 'write_file', 'arguments': '{'}})
                        error = {'type': 'response.failed', 'response': {'error': {'code': 'rate_limit_exceeded'}}}
                    else:
                        body = frame({'choices': [{'delta': {'content': 'partial draft', 'tool_calls': [{
                            'index': 0, 'id': 'w', 'function': {'name': 'write_file', 'arguments': '{'}}]}}]})
                        error = {'error': {'code': 'rate_limit_exceeded'}}
                    if failure == 'rate_limit':
                        body += frame(error)
                    def respond(request, count, clock):
                        return httpx.Response(200, headers={'Content-Type': 'text/event-stream'},
                                              stream=BrokenStream(body) if failure == 'read_error'
                                              else httpx.ByteStream(body))
                    with self.build(respond, stream=True, wire_api=wire) as (agent, clock, sent), \
                         patch('termux_agent.agent.execute', side_effect=AssertionError('draft tool executed')) as execute:
                        agent.run('task')
                        self.assertEqual(len(sent), 1)
                        self.assertEqual(clock.sleeps, [])
                        self.assertFalse(agent.last_run['error']['pre_accept'])
                        self.assertFalse(agent.last_run['error']['transient'])
                        self.assertEqual(agent.incomplete_draft['content'], 'partial draft')
                        self.assertEqual(agent.incomplete_draft['_incomplete_tool_calls'][0]['function']['name'], 'write_file')
                        self.assertNotIn('tool_calls', agent.incomplete_draft)
                        self.assertEqual(execute.call_count, 0)
                        self.assertNotIn('auto_resumes', agent.task_state)
                        validate_messages(agent.messages)

    def test_permanent_json_errors_and_unknown_accepted_failures_are_not_retried(self):
        for wire in ('chat_completions', 'responses'):
            for code in ('insufficient_quota', 'invalid_api_key', 'permission_denied', 'server_error'):
                with self.subTest(wire=wire, code=code):
                    def respond(request, count, clock):
                        body = {'error': {'code': code, 'message': 'not accepted as successful output'}}
                        if wire == 'responses':
                            body.update(status='failed', output=[])
                        return httpx.Response(200, json=body)
                    with self.build(respond, wire_api=wire) as (agent, clock, sent):
                        agent.run('task')
                        self.assertEqual((len(sent), clock.sleeps), (1, []))
                        self.assertFalse(agent.last_run['error']['pre_accept'])
                        self.assertFalse(agent.last_run['error']['transient'])

    def test_retry_after_survives_exhaustion_before_an_internal_wait(self):
        for attempts in (1, 3):
            with self.subTest(attempts=attempts):
                def respond(request, count, clock):
                    return (httpx.Response(503, headers={'Retry-After': '120'}) if count == 1 else answer())
                with self.build(respond, retry_max_attempts=attempts) as (agent, clock, sent):
                    self.assertEqual(agent.run('task'), 'done')
                    self.assertEqual(len(sent), 2)
                    self.assertGreaterEqual(sent[1][0] - sent[0][0], 120)
                    self.assertGreaterEqual(agent.task_state['active_seconds'], 120)
                    self.assertEqual(agent.task_state['auto_resumes'], 1)

    def test_retry_after_on_the_last_permitted_http_attempt_is_retained(self):
        def respond(request, count, clock):
            if count <= 3:
                return httpx.Response(503, headers={'Retry-After': '120'} if count == 3 else {})
            return answer()
        with self.build(respond, retry_max_attempts=3) as (agent, clock, sent):
            self.assertEqual(agent.run('task'), 'done')
            self.assertEqual(len(sent), 4)
            self.assertGreaterEqual(sent[3][0] - sent[2][0], 120)
            self.assertEqual(agent.task_state['logical_calls'], 2)
            self.assertEqual(agent._http_attempts_total, 4)

    def test_automatic_budget_renewal_preserves_shared_cooldown_in_its_checkpoint(self):
        call = fixtures.call('read', 'read_file', {'path': 'x'})
        def respond(request, count, clock):
            return answer('', [call]) if count == 1 else answer()
        with self.build(respond, max_task_seconds=200) as (agent, clock, sent):
            observed = []
            def execute(*args, **kwargs):
                clock.advance(201)
                agent.task_state['retry_not_before'] = clock.wall + 120
                agent.task_state['retry'] = {'phase': 'deferred', 'cause': 'shared-worker'}
                return {'content': 'new progress'}
            def sleep(seconds):
                observed.append(agent.task_state.get('retry_not_before'))
                clock.sleep(seconds)
            agent._sleep = sleep
            with patch('termux_agent.agent.execute', side_effect=execute):
                self.assertEqual(agent.run('task'), 'done')
            self.assertEqual(len(sent), 2)
            self.assertEqual(agent.task_state['renewals'], 1)
            self.assertEqual(observed, [1321.0])
            self.assertGreaterEqual(sent[1][0], 421)
            self.assertGreaterEqual(agent.task_state['active_seconds'], 120)

    def test_explicit_budget_renewal_preserves_cooldown_for_the_client_hook(self):
        with self.build(lambda *args: answer(), auto_continue=False, max_task_seconds=200,
                        retry_wait_budget=180) as (agent, clock, sent):
            agent.messages.append({'role': 'user', 'content': 'task'})
            agent._ensure_task_state('task')
            agent.task_state.update(active_seconds=201, retry_not_before=clock.wall + 120,
                                    retry={'phase': 'deferred'})
            agent.last_run.update(status='limit', stop_reason='time_budget', can_continue=True)
            self.assertEqual(agent.continue_task(), 'done [TASK_DONE]')
            self.assertEqual(clock.sleeps, [120])
            self.assertEqual(sent[0][0], 220)
            self.assertEqual(agent.task_state['renewals'], 1)
            self.assertGreaterEqual(agent.task_state['active_seconds'], 120)

    def test_outer_wait_cannot_bypass_a_one_second_owner_budget(self):
        def respond(*args):
            return httpx.Response(429, json={'error': {'code': 'rate_limit_exceeded'}}, headers={'Retry-After': '60'})
        with self.build(respond, max_task_seconds=1) as (agent, clock, sent):
            answer_text = agent.run('task')
            self.assertEqual((len(sent), clock.sleeps), (1, []))
            self.assertIn('剩余任务时间', answer_text)
            self.assertNotIn('auto_resumes', agent.task_state)

    def test_outer_wait_time_is_charged_and_checkpointed(self):
        def respond(request, count, clock):
            return (httpx.Response(503, headers={'Retry-After': '20'}) if count == 1 else answer())
        with self.build(respond, max_task_seconds=100) as (agent, clock, sent):
            saved = []
            agent.checkpoint = lambda: saved.append(agent.task_state['active_seconds'])
            self.assertEqual(agent.run('task'), 'done')
            self.assertGreaterEqual(agent.task_state['active_seconds'], 21)
            self.assertTrue(any(value >= 21 for value in saved))
            self.assertEqual(clock.sleeps, [21])

    def test_interrupted_outer_wait_preserves_elapsed_time_and_cooldown(self):
        def respond(*args):
            return httpx.Response(429, json={'error': {'code': 'rate_limit_exceeded'}}, headers={'Retry-After': '60'})
        with self.build(respond, max_task_seconds=100) as (agent, clock, sent):
            saved = []
            agent.checkpoint = lambda: saved.append(agent.task_state['active_seconds'])
            def interrupted(seconds):
                clock.advance(10)
                raise KeyboardInterrupt
            agent._sleep = interrupted
            with self.assertRaises(KeyboardInterrupt):
                agent.run('task')
            self.assertEqual(len(sent), 1)
            self.assertEqual(agent.last_run['status'], 'interrupted')
            self.assertEqual(agent.task_state['active_seconds'], 10)
            self.assertEqual(saved[-1], 10)
            self.assertEqual(agent.task_state['retry_not_before'], 1060)

    def test_transient_error_on_the_last_call_renews_before_clearing_progress(self):
        call = fixtures.call('read', 'read_file', {'path': 'x'})
        def respond(request, count, clock):
            if count == 1:
                return answer('', [call])
            if count == 2:
                return httpx.Response(429, json={'error': {'code': 'rate_limit_exceeded'}}, headers={'Retry-After': '30'})
            return answer()
        with self.build(respond, max_task_calls=2) as (agent, clock, sent), \
             patch('termux_agent.agent.execute', return_value={'content': 'new progress'}):
            self.assertEqual(agent.run('task'), 'done')
            self.assertEqual(len(sent), 3)
            self.assertEqual(agent.task_state['renewals'], 1)
            self.assertEqual(agent.task_state['logical_calls'], 1)
            self.assertGreaterEqual(sent[2][0] - sent[1][0], 30)
            self.assertGreater(agent.task_state['tokens'], agent.task_state['budget_windows'][0]['tokens'])

    def test_spent_token_budget_stops_before_waiting_after_a_transient_error(self):
        def respond(*args):
            return httpx.Response(429, json={'error': {'code': 'rate_limit_exceeded'}}, headers={'Retry-After': '30'})
        with self.build(respond, max_task_tokens=1) as (agent, clock, sent):
            answer_text = agent.run('task')
            self.assertEqual((len(sent), clock.sleeps), (1, []))
            self.assertIn('token 预算已用完', answer_text)
            self.assertNotIn('auto_resumes', agent.task_state)

    def test_unchanged_successes_do_not_reset_failure_count_or_backoff(self):
        calls = [fixtures.call('a', 'read_file', {'path': 'a'}), fixtures.call('b', 'read_file', {'path': 'b'})]
        def respond(request, count, clock):
            index = (count - 1) % 3
            return answer('', [calls[index]]) if index < 2 else httpx.Response(503)
        with self.build(respond) as (agent, clock, sent), \
             patch('termux_agent.agent.execute', return_value={'content': 'unchanged'}):
            answer_text = agent.run('task')
            self.assertEqual(len(sent), 15)
            self.assertEqual(clock.sleeps, [5, 15, 45, 135])
            self.assertIn('连续多次失败', answer_text)
            self.assertEqual(len(agent._seen_progress), 2)

    def test_non_main_thread_deadline_refusal_stops_without_http_or_waits(self):
        with self.build(lambda *args: answer()) as (agent, clock, sent):
            errors = []
            def run():
                try:
                    agent.run('task')
                except BaseException as exc:
                    errors.append(exc)
            thread = threading.Thread(target=run)
            thread.start()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual((len(sent), clock.sleeps), (0, []))
            self.assertEqual(agent.task_state['logical_calls'], 1)
            self.assertFalse(agent.last_run['error']['transient'])

    def test_existing_alarm_setup_refusal_stops_without_http_or_waits(self):
        with self.build(lambda *args: answer()) as (agent, clock, sent), \
             patch('termux_agent.retry.signal.getitimer', return_value=(10, 0)):
            agent.run('task')
            self.assertEqual((len(sent), clock.sleeps), (0, []))
            self.assertEqual(agent.task_state['logical_calls'], 1)
            self.assertFalse(agent.last_run['error']['transient'])

    def test_empty_stream_rate_limit_retries_same_wire_without_replaying_prior_tools(self):
        # Phone symptom: an accepted SSE with no body/thinking/tool fragments
        # fails immediately with a structured rate limit after a round of tools.
        calls = [fixtures.call('c%d' % i, 'read_file', {'path': 'p%d' % i}) for i in range(4)]
        def respond(request, count, clock):
            if count == 1:
                return sse_response(responses_completed('', calls))
            if count == 2:
                return sse_response(empty_rate_limit_failed())
            return sse_response(responses_completed('done'))
        with self.build(respond, stream=True, wire_api='responses', retry_max_attempts=3,
                        auto_continue=False) as (agent, clock, sent), \
             patch('termux_agent.agent.execute', return_value={'content': 'ok'}) as execute:
            self.assertEqual(agent.run('task'), 'done')
        self.assertEqual(len(sent), 3)
        self.assertNotEqual(sent[0][1], sent[1][1])   # a new logical step after the tools
        self.assertEqual(sent[1][1], sent[2][1])      # the bounded retry uses the identical wire
        self.assertEqual(execute.call_count, 4)       # previous round's tools executed exactly once
        self.assertNotIn('auto_resumes', agent.task_state)
        validate_messages(agent.messages)

    def test_empty_stream_metadata_and_error_event_variants_retry(self):
        created = frame({'type': 'response.created',
                         'response': {'id': 'r', 'status': 'in_progress', 'output': []}})
        in_progress = frame({'type': 'response.in_progress', 'response': {'id': 'r', 'output': []}})
        variants = {
            'bare-error': empty_rate_limit_error(),
            'created+failed': created + empty_rate_limit_failed(),
            'created+error': created + empty_rate_limit_error(),
            'in_progress+failed': in_progress + empty_rate_limit_failed(),
        }
        for label, failure in variants.items():
            with self.subTest(variant=label):
                def respond(request, count, clock):
                    return (sse_response(failure) if count == 1
                            else sse_response(responses_completed('done')))
                with self.build(respond, stream=True, wire_api='responses', retry_max_attempts=2,
                                auto_continue=False) as (agent, clock, sent):
                    self.assertEqual(agent.run('task'), 'done')
                self.assertEqual(len(sent), 2)
                self.assertEqual(sent[0][1], sent[1][1])  # same wire, no partial output to protect
                self.assertNotIn('auto_resumes', agent.task_state)

    def test_partial_or_unknown_events_never_retry_empty_stream_rate_limit(self):
        prefixes = {
            'body': frame({'type': 'response.output_text.delta', 'delta': 'partial draft'}),
            'thinking': frame({'type': 'response.reasoning_text.delta', 'delta': 'thinking'}),
            'tool_item': frame({'type': 'response.output_item.added', 'output_index': 0,
                                'item': {'type': 'function_call', 'call_id': 'w',
                                         'name': 'write_file', 'arguments': '{'}}),
            'tool_fragment': frame({'type': 'response.function_call_arguments.delta',
                                    'output_index': 0, 'delta': '{"a":'}),
            'unknown': frame({'type': 'response.custom.side_effect', 'value': 1}),
        }
        for label, prefix in prefixes.items():
            with self.subTest(label=label):
                body = prefix + empty_rate_limit_failed()
                def respond(request, count, clock):
                    return sse_response(body)
                with self.build(respond, stream=True, wire_api='responses', retry_max_attempts=3,
                                auto_continue=False) as (agent, clock, sent), \
                     patch('termux_agent.agent.execute', side_effect=AssertionError('draft tool executed')) as execute:
                    agent.run('task')
                self.assertEqual(len(sent), 1)          # a partial/unknown event forbids a replay
                self.assertEqual(clock.sleeps, [])
                self.assertFalse(agent.last_run['error']['pre_accept'])
                self.assertFalse(agent.last_run['error']['transient'])
                self.assertEqual(execute.call_count, 0)
                validate_messages(agent.messages)

    def test_empty_stream_non_rate_limit_error_stops_without_retry(self):
        body = frame({'type': 'response.failed', 'response': {
            'status': 'failed', 'error': {'code': 'server_error', 'message': 'boom'}}})
        def respond(request, count, clock):
            return sse_response(body)
        with self.build(respond, stream=True, wire_api='responses', retry_max_attempts=3,
                        auto_continue=False) as (agent, clock, sent):
            agent.run('task')
        self.assertEqual(len(sent), 1)
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(agent.last_run['error']['type'], 'ProviderError')

    def test_empty_stream_rate_limit_honours_retry_after_and_retry_after_ms(self):
        for headers, expected in (({'Retry-After': '7'}, 7.0),
                                  ({'retry-after-ms': '2500'}, 2.5)):
            with self.subTest(headers=headers):
                def respond(request, count, clock):
                    return (sse_response(empty_rate_limit_failed(), headers) if count == 1
                            else sse_response(responses_completed('done')))
                with self.build(respond, stream=True, wire_api='responses', retry_max_attempts=3,
                                auto_continue=False) as (agent, clock, sent):
                    self.assertEqual(agent.run('task'), 'done')
                self.assertEqual(len(sent), 2)
                self.assertGreaterEqual(sent[1][0] - sent[0][0], expected)
                self.assertTrue(any(s >= expected for s in clock.sleeps))
                self.assertNotIn('auto_resumes', agent.task_state)

    def test_exhausted_empty_stream_rate_limit_keeps_cooldown_and_blocks_outer_replay(self):
        def respond(request, count, clock):
            return (sse_response(empty_rate_limit_failed()) if count <= 3
                    else sse_response(responses_completed('done')))
        with self.build(respond, stream=True, wire_api='responses', retry_max_attempts=3,
                        auto_continue=True, auto_resume=True) as (agent, clock, sent):
            saved = []
            agent.checkpoint = lambda: saved.append(dict(agent.task_state))
            agent.run('task')
            self.assertEqual(len(sent), 3)                       # bounded, no unbounded outer replay
            self.assertEqual(agent.last_run['stop_reason'], 'rate_limit')
            self.assertFalse(agent.last_run['error']['pre_accept'])
            self.assertFalse(agent.last_run['error']['transient'])
            self.assertNotIn('auto_resumes', agent.task_state)
            self.assertGreater(agent.task_state['retry_not_before'], clock.wall)
            self.assertIn('retry_not_before', saved[-1])        # cooldown persisted in the checkpoint
            cooldown = agent.task_state['retry_not_before'] - clock.wall
            sleeps_before = len(clock.sleeps)
            self.assertEqual(agent.continue_task(auto_continue=False), 'done')
            self.assertTrue(any(s >= cooldown - 0.001 for s in clock.sleeps[sleeps_before:]))

    def test_empty_stream_wait_budget_exhaustion_keeps_a_usable_cooldown(self):
        def respond(request, count, clock):
            return sse_response(empty_rate_limit_failed(), {'Retry-After': '60'})
        with self.build(respond, stream=True, wire_api='responses', retry_max_attempts=3,
                        retry_wait_budget=1, auto_continue=False) as (agent, clock, sent):
            agent.run('task')
        self.assertEqual(len(sent), 1)
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(agent.last_run['stop_reason'], 'rate_limit')
        self.assertFalse(agent.last_run['error']['pre_accept'])
        self.assertGreater(agent.task_state['retry_not_before'], clock.wall)

    def test_interrupted_empty_stream_wait_preserves_cooldown_for_manual_resume(self):
        def respond(request, count, clock):
            return (sse_response(empty_rate_limit_failed()) if count == 1
                    else sse_response(responses_completed('done')))
        with self.build(respond, stream=True, wire_api='responses', retry_max_attempts=3,
                        auto_continue=False) as (agent, clock, sent):
            def interrupted(seconds):
                clock.advance(1)
                raise KeyboardInterrupt
            agent.client._sleep = interrupted
            agent.run('task')                          # inner wait interrupt is absorbed, task marked interrupted
            self.assertEqual(len(sent), 1)
            self.assertEqual(agent.last_run['status'], 'interrupted')
            self.assertGreater(agent.task_state['retry_not_before'], clock.wall)
            cooldown = agent.task_state['retry_not_before'] - clock.wall
            agent.client._sleep = clock.sleep
            sleeps_before = len(clock.sleeps)
            self.assertEqual(agent.continue_task(auto_continue=False), 'done')
            self.assertTrue(any(s >= cooldown - 0.001 for s in clock.sleeps[sleeps_before:]))

    def test_failure_frame_output_disqualifies_retry_and_is_preserved(self):
        # The failure frame's own response.output is partial work: no replay,
        # and the tool call must be kept as an inert draft for viewing.
        call = {'type': 'function_call', 'call_id': 'w', 'name': 'write_file',
                'arguments': '{"path":"x"}', 'status': 'completed'}
        body = frame({'type': 'response.failed', 'response': {
            'status': 'failed', 'error': {'code': 'rate_limit_exceeded'}, 'output': [call]}})
        def respond(request, count, clock):
            return sse_response(body)
        with self.build(respond, stream=True, wire_api='responses', retry_max_attempts=3,
                        auto_continue=False) as (agent, clock, sent), \
             patch('termux_agent.agent.execute', side_effect=AssertionError('draft tool executed')) as execute:
            agent.run('task')
        self.assertEqual(len(sent), 1)               # no second identical request
        self.assertEqual(clock.sleeps, [])
        self.assertFalse(agent.last_run['error']['pre_accept'])
        self.assertEqual(execute.call_count, 0)
        calls = agent.incomplete_draft.get('_incomplete_tool_calls', [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['function']['name'], 'write_file')

    def test_failure_frame_text_output_is_preserved_as_draft(self):
        text_item = responses_text_item('partial answer')
        body = frame({'type': 'response.failed', 'response': {
            'status': 'failed', 'error': {'code': 'rate_limit_exceeded'}, 'output': [text_item]}})
        def respond(request, count, clock):
            return sse_response(body)
        with self.build(respond, stream=True, wire_api='responses', retry_max_attempts=3,
                        auto_continue=False) as (agent, clock, sent):
            agent.run('task')
        self.assertEqual(len(sent), 1)
        self.assertIn('partial answer', agent.incomplete_draft.get('content', ''))

    def test_failure_frame_with_empty_output_still_retries(self):
        # An empty output list is not partial work; the stream is still eligible.
        body = frame({'type': 'response.failed', 'response': {
            'status': 'failed', 'error': {'code': 'rate_limit_exceeded'}, 'output': []}})
        def respond(request, count, clock):
            return (sse_response(body) if count == 1
                    else sse_response(responses_completed('done')))
        with self.build(respond, stream=True, wire_api='responses', retry_max_attempts=2,
                        auto_continue=False) as (agent, clock, sent):
            self.assertEqual(agent.run('task'), 'done')
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0][1], sent[1][1])

    def test_done_marker_closes_empty_stream_retry_eligibility(self):
        body = b'data: [DONE]\n\n' + empty_rate_limit_failed()
        def respond(request, count, clock):
            return sse_response(body)
        with self.build(respond, stream=True, wire_api='responses', retry_max_attempts=3,
                        auto_continue=False) as (agent, clock, sent):
            agent.run('task')
        self.assertEqual(len(sent), 1)
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(agent.last_run['stop_reason'], 'rate_limit')

    def test_non_string_event_type_is_unknown_and_disables_retry(self):
        cases = (
            ('completion', frame({'type': []}) + responses_completed('done')),
            ('failure', frame({'type': []}) + empty_rate_limit_failed()),
            ('dict_type', frame({'type': {}}) + responses_completed('done')),
        )
        for label, body in cases:
            with self.subTest(label=label):
                def respond(request, count, clock):
                    return sse_response(body)
                with self.build(respond, stream=True, wire_api='responses', retry_max_attempts=3,
                                auto_continue=False) as (agent, clock, sent):
                    result = agent.run('task')
                self.assertEqual(len(sent), 1)       # no crash, no replay
                self.assertEqual(clock.sleeps, [])
                if label == 'failure':
                    self.assertEqual(agent.last_run['stop_reason'], 'rate_limit')
                else:
                    self.assertEqual(result, 'done')  # next frame still decodes normally

    @unittest.skipUnless(hasattr(signal, 'setitimer') and hasattr(signal, 'ITIMER_REAL'),
                         'Unix real-time interval timers are required')
    def test_empty_stream_retry_restores_deadline_before_wait(self):
        # The re-armed signal timer must already be live during the wait's
        # checkpoint / on_retry callbacks, not only after the wait returns.
        observed = []
        def respond(request, count, clock):
            return (sse_response(empty_rate_limit_failed()) if count == 1
                    else sse_response(responses_completed('done')))
        with self.build(respond, stream=True, wire_api='responses', retry_max_attempts=3,
                        auto_continue=False) as (agent, clock, sent):
            agent.client.on_retry = lambda info: observed.append(
                signal.getitimer(signal.ITIMER_REAL)[0])
            self.assertEqual(agent.run('task'), 'done')
        self.assertEqual(len(observed), 1)
        self.assertGreater(observed[0], 0)           # deadline active during the callback

    def test_empty_stream_wait_budget_exhaustion_error_is_accepted_with_cooldown(self):
        def respond(request, count, clock):
            return sse_response(empty_rate_limit_failed(), {'Retry-After': '60'})
        with self.build(respond, stream=True, wire_api='responses', retry_max_attempts=3,
                        retry_wait_budget=1, auto_continue=False) as (agent, clock, sent):
            with self.assertRaises(RateLimitError) as caught:
                agent.client.chat([{'role': 'user', 'content': 'task'}], [])
            self.assertFalse(caught.exception.pre_accept)          # accepted, not pre-accept replay
            self.assertGreater(caught.exception.retry_after_seconds, 0)  # cooldown preserved
            self.assertTrue(agent.client.last_accepted)            # real stream state kept
        self.assertEqual(len(sent), 1)
        self.assertEqual(clock.sleeps, [])

    @unittest.skipUnless(hasattr(signal, 'setitimer') and hasattr(signal, 'ITIMER_REAL'),
                         'Unix real-time interval timers are required')
    def test_accepted_stream_wait_deadline_error_is_post_accept_and_keeps_cooldown(self):
        # HTTP 200 opened an empty stream; the total deadline interrupts the
        # empty-stream retry wait. The resulting NetworkError must reflect the
        # real post-accept phase and keep the cooldown from the interrupted wait.
        before_handler = signal.getsignal(signal.SIGALRM)
        before_timer = signal.getitimer(signal.ITIMER_REAL)
        if before_timer[0] > 0:
            self.skipTest('an unrelated process alarm is already active')

        def restore():
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, before_handler)
            signal.setitimer(signal.ITIMER_REAL, *before_timer)

        self.addCleanup(restore)
        seen = []

        def respond(request):
            seen.append(request)
            return sse_response(empty_rate_limit_failed(), {'retry-after-ms': '40'})

        with httpx.Client(transport=httpx.MockTransport(respond)) as http:
            cfg = Config(wire_api='responses', stream=True, retry_max_attempts=3,
                         retry_wait_budget=20, retry_total_timeout=0.07)
            client = Client(cfg, http_client=http, randomizer=lambda: 0)
            client.on_retry = lambda info: time.sleep(0.18)  # deadline fires inside the callback
            with self.assertRaises(NetworkError) as caught:
                client.chat([{'role': 'user', 'content': 'task'}], [])
            self.assertFalse(caught.exception.pre_accept)       # post-accept, not pre-accept replay
            self.assertTrue(client.last_accepted)               # real accepted state
            self.assertGreater(caught.exception.retry_after_seconds or 0, 0)  # cooldown preserved
        self.assertEqual(len(seen), 1)
        self.assertIs(signal.getsignal(signal.SIGALRM), before_handler)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))

    @unittest.skipUnless(hasattr(signal, 'setitimer') and hasattr(signal, 'ITIMER_REAL'),
                         'Unix real-time interval timers are required')
    def test_pre_receive_deadline_error_stays_pre_accept(self):
        # An ordinary pre-receive timeout (no response accepted yet) must keep
        # pre_accept=True; this guards the accepted-stream fix above.
        before_handler = signal.getsignal(signal.SIGALRM)
        before_timer = signal.getitimer(signal.ITIMER_REAL)
        if before_timer[0] > 0:
            self.skipTest('an unrelated process alarm is already active')

        def restore():
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, before_handler)
            signal.setitimer(signal.ITIMER_REAL, *before_timer)

        self.addCleanup(restore)
        seen = []

        def respond(request):
            seen.append(request)
            time.sleep(0.18)
            return sse_response(responses_completed('done'))

        with httpx.Client(transport=httpx.MockTransport(respond)) as http:
            cfg = Config(wire_api='responses', stream=True, retry_max_attempts=1,
                         retry_total_timeout=0.07)
            client = Client(cfg, http_client=http)
            with self.assertRaises(NetworkError) as caught:
                client.chat([{'role': 'user', 'content': 'task'}], [])
            self.assertTrue(caught.exception.pre_accept)
            self.assertFalse(client.last_accepted)
        self.assertEqual(len(seen), 1)
        self.assertIs(signal.getsignal(signal.SIGALRM), before_handler)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))

    def test_failure_frame_output_merges_with_deltas_and_stays_inert(self):
        # Text delta and tool item arrive first; response.failed repeats the
        # same text and call_id. The draft must show each exactly once.
        body = (
            frame({'type': 'response.output_text.delta', 'output_index': 0, 'delta': 'partial answer'}) +
            frame({'type': 'response.output_item.added', 'output_index': 1, 'item': {
                'type': 'function_call', 'call_id': 'call_1', 'name': 'read_file', 'arguments': ''}}) +
            frame({'type': 'response.function_call_arguments.delta', 'output_index': 1,
                   'delta': '{"path":'}) +
            frame({'type': 'response.failed', 'response': {
                'status': 'failed', 'error': {'code': 'rate_limit_exceeded'},
                'output': [
                    {'type': 'message', 'id': 'msg_1', 'role': 'assistant',
                     'content': [{'type': 'output_text', 'text': 'partial answer'}]},
                    {'type': 'function_call', 'call_id': 'call_1', 'name': 'read_file',
                     'arguments': '{"path":"/tmp/a"}'},
                ]}})
        )

        def respond(request, count, clock):
            return sse_response(body)

        with self.build(respond, stream=True, wire_api='responses', retry_max_attempts=3,
                        auto_continue=False) as (agent, clock, sent), \
             patch('termux_agent.agent.execute', side_effect=AssertionError('draft tool executed')) as execute:
            agent.run('task')
        self.assertEqual(len(sent), 1)                 # partial stream still forbids retry
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(execute.call_count, 0)
        draft = agent.incomplete_draft
        self.assertEqual(draft['content'], 'partial answer')   # no duplicated text
        self.assertNotIn('tool_calls', draft)                  # inert draft
        calls = draft.get('_incomplete_tool_calls', [])
        self.assertEqual(len(calls), 1)                       # no duplicated tool
        self.assertEqual(calls[0]['id'], 'call_1')
        self.assertEqual(calls[0]['function']['arguments'], '{"path":"/tmp/a"}')

    def test_post_accept_connection_timeout_is_not_pre_accept(self):
        # An empty accepted stream is retried; the second connection times out.
        def respond(request, count, clock):
            if count == 1:
                return sse_response(empty_rate_limit_failed())
            raise httpx.ReadTimeout('retry connection stalled')

        with self.build(respond, stream=True, wire_api='responses', retry_max_attempts=2,
                        auto_continue=False) as (agent, clock, sent):
            with self.assertRaises(NetworkError) as caught:
                agent.client.chat([{'role': 'user', 'content': 'task'}], [])
            self.assertFalse(caught.exception.pre_accept)   # post-accept, not a replay
            self.assertTrue(caught.exception.transient)      # original flag preserved
            self.assertTrue(agent.client.last_accepted)
        self.assertEqual(len(sent), 2)

    def test_post_accept_structured_429_is_not_pre_accept_and_keeps_cooldown(self):
        # Second connection returns HTTP 429 with Retry-After: 8 on the last
        # permitted attempt; the surfaced RateLimitError must stay truthful.
        def respond(request, count, clock):
            if count == 1:
                return sse_response(empty_rate_limit_failed())
            return httpx.Response(429, json={'error': {'code': 'rate_limit_exceeded'}},
                                  headers={'Retry-After': '8'})

        with self.build(respond, stream=True, wire_api='responses', retry_max_attempts=2,
                        auto_continue=False) as (agent, clock, sent):
            with self.assertRaises(RateLimitError) as caught:
                agent.client.chat([{'role': 'user', 'content': 'task'}], [])
            self.assertFalse(caught.exception.pre_accept)
            self.assertEqual(caught.exception.retry_after_seconds, 8)  # cooldown preserved
            self.assertTrue(agent.client.last_accepted)
        self.assertEqual(len(sent), 2)

    def test_post_accept_clock_deadline_after_wait_is_not_pre_accept(self):
        # The monotonic clock passes the total deadline during the empty-stream
        # retry wait; the next loop iteration rejects before sending again.
        def respond(request, count, clock):
            if count == 1:
                return sse_response(empty_rate_limit_failed())
            return sse_response(responses_completed('done'))

        with self.build(respond, stream=True, wire_api='responses', retry_max_attempts=3,
                        retry_total_timeout=5, auto_continue=False) as (agent, clock, sent):
            agent.client._sleep = lambda delay: clock.advance(10)  # clock jumps past deadline
            with self.assertRaises(NetworkError) as caught:
                agent.client.chat([{'role': 'user', 'content': 'task'}], [])
            self.assertFalse(caught.exception.pre_accept)
            self.assertTrue(agent.client.last_accepted)
        self.assertEqual(len(sent), 1)

    def test_pre_accept_failure_keeps_pre_accept_true(self):
        # No response accepted yet: the unified exit must not touch pre_accept.
        def respond(request, count, clock):
            raise httpx.ReadTimeout('no response yet')

        with self.build(respond, stream=True, wire_api='responses', retry_max_attempts=1,
                        auto_continue=False) as (agent, clock, sent):
            with self.assertRaises(NetworkError) as caught:
                agent.client.chat([{'role': 'user', 'content': 'task'}], [])
            self.assertTrue(caught.exception.pre_accept)
            self.assertFalse(agent.client.last_accepted)
        self.assertEqual(len(sent), 1)


if __name__ == '__main__':
    unittest.main()
