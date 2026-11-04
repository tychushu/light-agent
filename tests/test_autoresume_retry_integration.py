"""The outer automatic driver must preserve the real Client's retry safety contract."""
from contextlib import contextmanager
import json
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

import httpx
import test_mimo_acceptance as fixtures
from termux_agent.agent import Agent
from termux_agent.client import Client
from termux_agent.config import Config
from termux_agent.sessions import validate_messages


def frame(value):
    return ('data: ' + json.dumps(value) + '\n\n').encode()


def answer(content='done [TASK_DONE]', calls=None):
    message = {'content': content}
    if calls:
        message['tool_calls'] = calls
    return httpx.Response(200, json={'choices': [{'message': message, 'finish_reason': 'stop'}]})


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


if __name__ == '__main__':
    unittest.main()
