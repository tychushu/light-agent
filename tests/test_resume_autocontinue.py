"""A bare "继续执行" resumes the task; automatic tasks recover from recoverable stops by themselves."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_mimo_acceptance as fixtures
from termux_agent.agent import Agent
from termux_agent.client import (ClientError, ContextLengthError, NetworkError, ProviderError,
                                 RateLimitError)
from termux_agent.config import Config
from termux_agent.context import (compact_context_messages, compact_task_messages,
                                  compact_tool_history_messages, context_chars, task_starts)
from termux_agent.continuation import is_resume_text, is_task_start
from termux_agent.sessions import validate_messages, validate_task_state


def tool_reply(index, path=None):
    return {'tool_calls': [fixtures.call(f'c{index}', 'read_file', {'path': path or f'file-{index}.txt'})]}


def fake_execute(name, args, limit, **kwargs):
    # The result depends on the path, so different calls give different evidence.
    return {'content': 'data ' * 1200 + str(args.get('path'))}


def make_group(index, size=8000):
    return [{'role': 'assistant', 'content': 'inspect',
             'tool_calls': [fixtures.call(f'g{index}', 'read_file', {'path': f'f{index}.txt'})]},
            fixtures.tool_result(f'g{index}', {'content': 'data ' * (size // 5), 'execution_status': 'executed'})]


class Base(unittest.TestCase):
    def build(self, replies, **changes):
        changes.setdefault('auto_continue', True)
        changes.setdefault('auto_compact', False)
        agent = Agent(Config(**changes), client=fixtures.ScriptedClient(replies), base_system='rules')
        agent.events = []
        agent.event = lambda kind, payload: agent.events.append((kind, payload))
        agent.sleeps = []
        agent._sleep = agent.sleeps.append
        return agent

    def kinds(self, agent, kind):
        return [payload for name, payload in agent.events if name == kind]


class ResumeMatcherTests(unittest.TestCase):
    def test_only_a_bare_keep_going_message_is_a_resume(self):
        for text in ('继续执行', '继续', '  继续执行！ ', '请继续。', '接着做', 'Continue.', 'GO ON', 'keep going', '继续执行'):
            self.assertTrue(is_resume_text(text), text)
        for text in ('继续执行，但先备份', 'continue with file two', '帮我继续写文档', '继续执行 继续执行',
                     '"continue"', "'继续执行'", '`resume`', '“继续”', '「continue」', '＂continue＂',
                     '', '   ', 'x' * 500, None, 5, '继续 ' + 'a' * 60):
            self.assertFalse(is_resume_text(text), repr(text))

    def test_task_start_excludes_control_notes_and_resume_messages(self):
        self.assertTrue(is_task_start('update hermes'))
        for text in ('继续执行', '[TASK_RESUME]\nx', '[Context archive: a] {}', '[Task memo: {}]', 'continue'):
            self.assertFalse(is_task_start(text), text)

    def test_task_starts_ignore_fillers_but_legacy_histories_still_compact(self):
        messages = [{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': 'real task'},
                    {'role': 'user', 'content': '继续执行'}, {'role': 'user', 'content': '继续执行'},
                    {'role': 'user', 'content': '[TASK_RESUME]\nx'}]
        self.assertEqual(task_starts(messages), [1])
        only_fillers = [{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': '继续执行'}]
        self.assertEqual(task_starts(only_fillers), [1])


class ContextBoundaryTests(unittest.TestCase):
    def history(self, groups, tail):
        messages = [{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': 'update hermes'}]
        for index in range(groups):
            messages.extend(make_group(index))
        messages.extend({'role': 'user', 'content': text} for text in tail)
        return messages

    def test_resume_message_does_not_turn_earlier_work_into_old_history(self):
        messages = self.history(6, ['继续执行'])
        reduced, details = compact_tool_history_messages(messages, 1, 'archive')
        self.assertTrue(details['skipped_no_reduction'])
        self.assertEqual(reduced, messages)
        # A real follow-up instruction is a boundary: the earlier groups do reduce.
        real = self.history(6, ['continue, but back up the config first'])
        reduced, details = compact_tool_history_messages(real, 1, 'archive')
        self.assertEqual(details['omitted_groups'], 5)
        validate_messages(reduced)

    def test_task_memo_anchors_on_the_real_goal(self):
        messages = self.history(5, ['继续执行', '继续执行'])
        reduced, details = compact_task_messages(messages, 2, 'archive')
        self.assertEqual(reduced[1]['content'], 'update hermes')
        memo = next(m for m in reduced if m['content'].startswith('[Task memo: '))
        value = json.loads(memo['content'][len('[Task memo: '):-1])
        self.assertEqual(value['original_goal_ref']['message_index'], 1)
        self.assertEqual(details['omitted_groups'], 3)

    def test_context_reduction_keeps_every_human_message_including_resumes(self):
        messages = self.history(6, ['继续执行', '继续执行'])
        reduced, _ = compact_context_messages(messages, 2, 'archive')
        humans = [m['content'] for m in reduced if m['role'] == 'user' and not m['content'].startswith('[')]
        self.assertEqual(humans, ['update hermes', '继续执行', '继续执行'])


class RoutingTests(Base):
    def stop_at_step_limit(self, **changes):
        # Four tools run, the fifth batch hits the per-segment request limit.
        agent = self.build([tool_reply(i) for i in range(1, 6)], max_steps=5, auto_resume=False,
                           auto_compact=True, tool_history_keep_groups=1, **changes)
        with patch('termux_agent.agent.execute', side_effect=fake_execute):
            agent.run('update hermes')
        self.assertEqual(agent.last_run['stop_reason'], 'step_limit')
        return agent

    def test_bare_continue_resumes_the_same_task_and_keeps_all_work(self):
        agent = self.stop_at_step_limit()
        task_id, tools_before = agent.task_state['id'], sum(m['role'] == 'tool' for m in agent.messages)
        agent.client = fixtures.ScriptedClient([{'content': 'all done [TASK_DONE]'}])
        self.assertEqual(agent.run('继续执行'), 'all done')
        sent = agent.client.sent[0]
        self.assertEqual(agent.task_state['id'], task_id)
        self.assertEqual(agent.task_state['goal'], 'update hermes')
        self.assertEqual(sum(m['role'] == 'tool' for m in sent), tools_before)
        self.assertEqual(agent.in_memory_archives, [])
        self.assertTrue(sent[-1]['content'].startswith('[TASK_RESUME]\n'))
        self.assertIn('Original goal (truncated): update hermes', sent[-1]['content'])
        self.assertNotIn('继续执行', [m.get('content') for m in agent.messages])
        self.assertEqual(self.kinds(agent, 'resume_routed'), [{'goal': 'update hermes'}])
        self.assertEqual(agent.last_run['status'], 'complete')
        validate_messages(agent.messages)

    def test_a_real_instruction_that_starts_with_continue_is_a_new_task(self):
        agent = self.stop_at_step_limit()
        task_id = agent.task_state['id']
        agent.client = fixtures.ScriptedClient([{'content': 'ok [TASK_DONE]'}])
        agent.run('继续执行，但先备份配置')
        self.assertNotEqual(agent.task_state['id'], task_id)
        self.assertEqual(agent.task_state['goal'], '继续执行，但先备份配置')
        self.assertEqual(len(agent.in_memory_archives), 1)  # older tool history was reduced
        self.assertEqual(self.kinds(agent, 'resume_routed'), [])

    def test_after_a_finished_task_continue_is_a_message_that_keeps_the_goal_and_history(self):
        agent = self.build([tool_reply(1), tool_reply(2), tool_reply(3), {'content': 'done [TASK_DONE]'}],
                           auto_compact=True, tool_history_keep_groups=1)
        with patch('termux_agent.agent.execute', side_effect=fake_execute):
            agent.run('update hermes')
        first_id = agent.task_state['id']
        agent.client = fixtures.ScriptedClient([{'content': 'anything else? [TASK_DONE]'}])
        agent.run('继续')
        self.assertNotEqual(agent.task_state['id'], first_id)
        self.assertEqual(agent.task_state['goal'], 'update hermes')
        self.assertEqual(sum(m['role'] == 'tool' for m in agent.client.sent[0]), 3)
        self.assertEqual(agent.in_memory_archives, [])
        self.assertEqual(agent.client.sent[0][-1]['content'], '继续')

    def test_a_blocked_task_cannot_be_restarted_by_typing_bare_continue(self):
        agent = self.stop_at_step_limit()
        agent.last_run.update(status='blocked', stop_reason='approval_denied', can_continue=False)
        agent.client = fixtures.ScriptedClient([{'content': 'must not be requested [TASK_DONE]'}])
        state, messages = copy.deepcopy(agent.task_state), copy.deepcopy(agent.messages)
        for text in ('继续执行', 'continue', 'resume'):
            with self.assertRaises(ValueError):
                agent.run(text)
        self.assertEqual(agent.client.sent, [])
        self.assertEqual((agent.task_state, agent.messages), (state, messages))
        self.assertEqual(self.kinds(agent, 'resume_routed'), [])

    def test_resume_repairs_a_goal_that_an_older_version_stored_as_continue(self):
        agent = self.stop_at_step_limit()
        agent.task_state['goal'] = '继续执行'
        agent.client = fixtures.ScriptedClient([{'content': 'x [TASK_DONE]'}])
        agent.run('继续')
        self.assertEqual(agent.task_state['goal'], 'update hermes')

    def test_restored_legacy_task_uses_the_real_instruction_as_goal(self):
        agent = self.build([{'content': 'x [TASK_DONE]'}])
        agent.messages = [{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': 'update hermes'},
                          *make_group(1), {'role': 'user', 'content': '继续执行'}]
        agent.last_run = {'status': 'limit', 'stop_reason': 'step_limit', 'can_continue': True}
        agent.continue_task()
        self.assertEqual(agent.task_state['goal'], 'update hermes')

    def test_user_resume_reopens_a_spent_budget_window_under_current_limits(self):
        agent = self.build([tool_reply(1), tool_reply(2)], max_task_calls=2, max_steps=10, auto_resume=False)
        with patch('termux_agent.agent.execute', side_effect=fake_execute):
            agent.run('long task')
        self.assertEqual(agent.last_run['stop_reason'], 'task_call_budget')
        agent.config.max_task_calls = 5
        agent.client = fixtures.ScriptedClient([{'content': 'finished [TASK_DONE]'}])
        self.assertEqual(agent.run('继续执行'), 'finished')
        state = agent.task_state
        self.assertEqual((state['renewals'], state['logical_calls'], state['max_logical_calls']), (1, 1, 5))
        self.assertEqual(state['budget_windows'][0]['logical_calls'], 2)
        self.assertEqual(state['budget_windows'][0]['stop_reason'], 'task_call_budget')

    def test_user_resume_reopens_the_stall_guard(self):
        agent = self.stop_at_step_limit()
        agent._no_progress_stop = True
        agent._same_tool_fingerprint_count = 9
        agent.client = fixtures.ScriptedClient([{'content': 'x [TASK_DONE]'}])
        agent.continue_task()
        self.assertFalse(agent._no_progress_stop)


class AutoResumeTests(Base):
    def run_task(self, agent, text='long task'):
        with patch('termux_agent.agent.execute', side_effect=fake_execute):
            return agent.run(text)

    def test_step_limit_resumes_while_progress_is_new(self):
        agent = self.build([tool_reply(1), tool_reply(2), tool_reply(3), {'content': 'finished [TASK_DONE]'}],
                           max_steps=2, max_task_calls=50)
        self.assertEqual(self.run_task(agent), 'finished')
        self.assertEqual(agent.last_run['status'], 'complete')
        self.assertEqual(agent.task_state['auto_resumes'], 1)
        self.assertEqual(agent.last_run['auto_resumes'], 1)
        event, = self.kinds(agent, 'auto_resume')
        self.assertEqual((event['count'], event['max'], event['reason'], event['wait_seconds']),
                         (1, 8, 'step_limit', 0.0))
        self.assertEqual(agent.sleeps, [])
        validate_messages(agent.messages)

    def test_call_budget_window_renews_and_keeps_the_token_total(self):
        agent = self.build([tool_reply(1), tool_reply(2), {'content': 'finished [TASK_DONE]'}],
                           max_task_calls=2, max_steps=10, max_task_tokens=900_000)
        self.assertEqual(self.run_task(agent), 'finished')
        state = agent.task_state
        self.assertEqual(state['renewals'], 1)
        self.assertEqual(state['logical_calls'], 1)
        self.assertEqual(state['budget_windows'][0]['logical_calls'], 2)
        self.assertGreater(state['tokens'], state['budget_windows'][0]['tokens'])  # tokens are never reset

    def test_stops_with_a_reason_when_the_last_segment_found_nothing_new(self):
        same = {'tool_calls': [fixtures.call('c', 'read_file', {'path': 'same.txt'})]}
        agent = self.build([same] * 4, max_steps=2, max_task_calls=50)
        answer = self.run_task(agent)
        self.assertEqual(len(agent.client.sent), 4)
        self.assertEqual(agent.task_state['auto_resumes'], 1)
        self.assertIn('没有产生新的有效进展', answer)
        self.assertEqual(agent.last_run['auto_resume_note'], answer.splitlines()[-1])

    def test_english_notes_are_translated(self):
        same = {'tool_calls': [fixtures.call('c', 'read_file', {'path': 'same.txt'})]}
        agent = self.build([same] * 4, max_steps=2, ui_language='en')
        self.assertIn('made no new progress', self.run_task(agent))

    def test_resume_count_is_capped(self):
        agent = self.build([tool_reply(i) for i in range(1, 8)], max_steps=2, max_task_calls=50,
                           auto_resume_max=2)
        answer = self.run_task(agent)
        self.assertEqual(agent.task_state['auto_resumes'], 2)
        self.assertEqual(len(agent.client.sent), 6)
        self.assertIn('已达上限 2 次', answer)

    def test_transient_network_error_is_retried_after_a_backoff(self):
        agent = self.build([{'_raise': NetworkError('LLM request failed after retries')},
                            {'content': 'recovered [TASK_DONE]'}])
        self.assertEqual(agent.run('task'), 'recovered')
        self.assertEqual(agent.sleeps, [5.0])
        self.assertEqual(agent.task_state['auto_resumes'], 1)
        validate_messages(agent.messages)

    def test_repeated_failures_back_off_then_stop(self):
        agent = self.build([{'_raise': NetworkError('down')}] * 6)
        answer = agent.run('task')
        self.assertEqual(agent.sleeps, [5.0, 15.0, 45.0, 135.0])
        self.assertEqual(len(agent.client.sent), 5)
        self.assertIn('连续多次失败', answer)
        self.assertEqual(agent.last_run['stop_reason'], 'network_error')

    def test_only_classified_pre_accept_failures_are_transient(self):
        transient = [ProviderError('LLM endpoint returned HTTP 502 (example/v1/responses)')]
        for error in transient:
            agent = self.build([{'_raise': error}, {'content': 'ok [TASK_DONE]'}])
            self.assertEqual(agent.run('task'), 'ok', str(error))
        permanent = [ProviderError('Responses 流式请求失败（上游未提供错误详情，事件 response.failed）'),
                     ClientError('Responses 流在完成事件前结束；未执行其中的工具调用。'),
                     ProviderError('invalid_api_key'), NetworkError('accepted stream failed', pre_accept=False),
                     ProviderError('LLM endpoint returned HTTP 401 (example/v1/responses)'),
                     ProviderError('LLM endpoint returned HTML instead of API data (possible gateway/WAF response)'),
                     ProviderError('LLM endpoint returned HTTP 400 (message=content-blocked)'),
                     ClientError('Invalid Responses output index')]
        for error in permanent:
            agent = self.build([{'_raise': error}, {'content': 'must not be requested [TASK_DONE]'}])
            agent.run('task')
            self.assertEqual(len(agent.client.sent), 1, str(error))
            self.assertEqual(agent.last_run['error']['transient'], False)
            self.assertNotIn('auto_resumes', agent.task_state)

    def test_error_details_are_recorded_for_diagnosis(self):
        agent = self.build([{'_raise': ProviderError('LLM endpoint returned HTTP 503 (example/v1)')}] * 2,
                           auto_resume=False)
        agent.run('task')
        self.assertEqual(agent.last_run['error'], {'type': 'ProviderError', 'status': 503, 'transient': True,
                                                   'calls_since_compaction': None})

    def test_rate_limit_waits_for_the_server_cooldown(self):
        agent = self.build([{'_raise': RateLimitError('slow down', retry_after_seconds=30)},
                            {'content': 'ok [TASK_DONE]'}])
        self.assertEqual(agent.run('task'), 'ok')
        self.assertEqual(len(agent.sleeps), 1)
        self.assertTrue(30.0 < agent.sleeps[0] <= 31.0, agent.sleeps)

    def test_a_cooldown_longer_than_the_wait_limit_is_left_to_the_user(self):
        agent = self.build([{'_raise': RateLimitError('slow down', retry_after_seconds=900)}] * 2)
        answer = agent.run('task')
        self.assertEqual((agent.sleeps, len(agent.client.sent)), ([], 1))
        self.assertIn('auto_resume_max_wait', answer)

    def test_a_person_is_needed_for_denials_budgets_and_context_limits(self):
        target = Path(tempfile.gettempdir()) / 'never-written-by-resume-test'
        denied = self.build([{'tool_calls': [fixtures.call('w', 'write_file', {'path': str(target), 'content': 'x'})]},
                             {'content': 'I could not write the file.'}], approval_policy='always')
        denied.approve = lambda name, args: False
        denied.run('task')
        # The model sees the denial once (two requests); nothing is requested after that.
        self.assertEqual((denied.last_run['stop_reason'], denied.last_run['can_continue'],
                          len(denied.client.sent)), ('approval_denied', False, 2))
        self.assertNotIn('auto_resumes', denied.task_state)
        self.assertFalse(target.exists())
        tokens = self.build([tool_reply(1), {'content': 'must not be requested'}], max_task_tokens=1)
        self.run_task(tokens)
        self.assertEqual((tokens.last_run['stop_reason'], len(tokens.client.sent)), ('token_budget', 1))
        context = self.build([{'_raise': ContextLengthError('too long')}, {'content': 'must not be requested'}])
        context.run('task')
        self.assertEqual((context.last_run['stop_reason'], len(context.client.sent)), ('context_limit', 1))

    def test_disabled_unless_automatic_and_enabled(self):
        for changes in ({'auto_continue': False}, {'auto_resume': False}, {'auto_resume_max': 0}):
            agent = self.build([{'_raise': NetworkError('down')}, {'content': 'must not be requested'}], **changes)
            agent.run('task')
            self.assertEqual((len(agent.client.sent), agent.sleeps), (1, []), changes)

    def test_waiting_can_be_interrupted_and_the_task_stays_resumable(self):
        agent = self.build([{'_raise': NetworkError('down')}, {'content': 'later [TASK_DONE]'}])
        def interrupted(seconds):
            raise KeyboardInterrupt
        agent._sleep = interrupted
        with self.assertRaises(KeyboardInterrupt):
            agent.run('task')
        self.assertEqual((agent.last_run['status'], agent.last_run['can_continue']), ('interrupted', True))
        agent._sleep = agent.sleeps.append
        self.assertEqual(agent.run('继续执行'), 'later')

    def test_state_is_saved_before_a_long_wait(self):
        agent = self.build([{'_raise': NetworkError('down')}, {'content': 'ok [TASK_DONE]'}])
        order = []
        agent.checkpoint = lambda: order.append('checkpoint')
        agent._sleep = lambda seconds: order.append(('sleep', seconds))
        agent.run('task')
        self.assertLess(order.index('checkpoint'), order.index(('sleep', 5.0)))
        self.assertEqual(agent.task_state['auto_resumes'], 1)

    def test_sub_agents_never_resume_on_their_own(self):
        agent = self.build([{'_raise': NetworkError('down')}, {'content': 'must not be requested'}])
        agent.run('task', auto_continue=False)
        self.assertEqual(len(agent.client.sent), 1)


class ResumePersistenceTests(Base):
    def test_final_auto_resume_count_is_in_the_last_checkpoint(self):
        agent = self.build([{'_raise': NetworkError('down')}, {'content': 'ok [TASK_DONE]'}])
        saved = []
        agent.checkpoint = lambda: saved.append(copy.deepcopy(agent.last_run))
        self.assertEqual(agent.run('task'), 'ok')
        self.assertEqual(saved[-1]['auto_resumes'], 1)
        self.assertEqual(saved[-1]['message'], 'ok')
        self.assertEqual(saved[-1], agent.last_run)

    def test_auto_resume_stop_note_and_returned_answer_are_checkpointed(self):
        same = {'tool_calls': [fixtures.call('c', 'read_file', {'path': 'same.txt'})]}
        agent = self.build([same] * 4, max_steps=2, max_task_calls=50)
        saved = []
        agent.checkpoint = lambda: saved.append(copy.deepcopy(agent.last_run))
        with patch('termux_agent.agent.execute', side_effect=fake_execute):
            answer = agent.run('task')
        self.assertIn('没有产生新的有效进展', answer)
        self.assertEqual(saved[-1]['message'], answer)
        self.assertEqual(saved[-1]['auto_resume_note'], answer.splitlines()[-1])
        self.assertEqual(saved[-1]['auto_resumes'], 1)

    def test_legacy_resume_filler_does_not_hide_a_prior_approval_denial(self):
        agent = self.build([])
        agent.messages.extend([
            {'role': 'user', 'content': 'real task'},
            {'role': 'assistant', 'tool_calls': [fixtures.call('w', 'write_file', {'path': 'x', 'content': 'x'})]},
            fixtures.tool_result('w', {'error': 'approval denied', 'execution_status': 'denied'}),
            {'role': 'user', 'content': '继续执行'},
        ])
        agent._mark_interrupted('recovered checkpoint')
        self.assertFalse(agent.last_run['can_continue'])
        self.assertEqual(agent.task_state['goal'], 'real task')
        agent.messages.append({'role': 'user', 'content': 'a different task'})
        self.assertFalse(agent._task_has_denial())

    def test_legacy_resume_filler_does_not_reset_the_delegate_count(self):
        agent = self.build([])
        agent.messages.extend([
            {'role': 'user', 'content': 'real task'},
            {'role': 'assistant', 'tool_calls': [fixtures.call('d', 'delegate', {'task': 'inspect'})]},
            fixtures.tool_result('d', {'status': 'complete', 'summary': 'found it'}),
            {'role': 'user', 'content': '继续执行'},
        ])
        agent._restore_legacy_task()
        self.assertEqual(agent.task_state['subagents_started'], 1)
        agent.messages.append({'role': 'user', 'content': 'a different task'})
        self.assertEqual(agent._task_delegate_count(), 0)

    def test_restored_task_does_not_call_recent_evidence_new_progress(self):
        from termux_agent.sessions import restore, snapshot
        same = {'tool_calls': [fixtures.call('c', 'read_file', {'path': 'same.txt'})]}
        with tempfile.TemporaryDirectory() as td:
            config_path = Path(td) / 'config.toml'
            config_path.write_text('model="test-model"\nauto_continue=true\nauto_resume=false\n'
                                   'auto_compact=false\nmax_steps=2\n')
            agent = Agent(Config.load(config_path), client=fixtures.ScriptedClient([same] * 2), base_system='rules')
            with patch('termux_agent.agent.execute', side_effect=fake_execute):
                agent.run('real task')
            scope = {'directory': td, 'config_path': str(config_path), 'instructions': '',
                     'session_file': None, 'disabled': True, 'instruction_path': None}
            client = fixtures.ScriptedClient([same, same, {'content': 'must not be requested [TASK_DONE]'}])
            def build(config, instructions, **kwargs):
                return Agent(config, client=client, **kwargs)
            config, restored, _ = restore(snapshot(agent, scope), build)
            config.auto_resume = True
            self.assertEqual(restored._seen_progress, set())  # reconstructed at user-resume time
            with patch('termux_agent.agent.execute', side_effect=fake_execute):
                answer = restored.continue_task()
            self.assertEqual(len(client.sent), 2)
            self.assertIn('没有产生新的有效进展', answer)
            self.assertNotIn('auto_resumes', restored.task_state)
            self.assertEqual(restored.task_state['id'], agent.task_state['id'])
            restored.close()
            agent.close()


class CliResumeTests(unittest.TestCase):
    """The production failure, end to end: persisted sessions, the real REPL, typed "继续执行"."""

    def run_cli(self, typed, replies, settings):
        import io
        import os
        import sqlite3
        from termux_agent import cli
        with tempfile.TemporaryDirectory() as home:
            root = Path(home)
            files = []
            for index in range(1, 6):
                path = root / f'file{index}.txt'
                path.write_text(f'line {index}\n' * 900)
                files.append(path)
            config = root / 'config.toml'
            config.write_text(settings)
            replies = [{'tool_calls': [fixtures.call(f'c{i}', 'read_file', {'path': str(path)})]}
                       for i, path in enumerate(files, 1)] + replies
            clients = []

            class FakeClient(fixtures.ScriptedClient):
                def __init__(self, cfg):
                    super().__init__(replies)
                    clients.append(self)

            previous = Path.cwd()
            try:
                with patch.dict(os.environ, {'HOME': home}), \
                     patch('termux_agent.agent.Client', FakeClient), \
                     patch.object(cli.sys, 'argv', ['ta', '--no-agent', '--config', str(config),
                                                    '--directory', str(root)]), \
                     patch.object(cli.sys.stdin, 'isatty', return_value=True), \
                     patch('builtins.input', side_effect=[*typed, '/exit']), \
                     patch('sys.stdout', new_callable=io.StringIO) as output:
                    cli.main()
                    shown = output.getvalue()
            finally:
                os.chdir(previous)
            db = sqlite3.connect(root / '.local/share/termux-agent/sessions.sqlite3')
            try:
                rows = db.execute('select id, title from sessions').fetchall()
            finally:
                db.close()
            return shown, rows, clients[0]

    settings = ('auto_continue = true\nauto_resume = false\nmax_steps = 5\nauto_compact = true\n'
                'tool_history_keep_groups = 1\napproval_policy = "never"\n')

    def test_typing_continue_keeps_one_session_with_all_tool_history(self):
        shown, rows, client = self.run_cli(['update hermes', '继续执行'],
                                           [{'content': 'finished [TASK_DONE]'}], self.settings)
        self.assertEqual(len(rows), 1, rows)  # the old behaviour branched a "· compact task" session
        self.assertIn('finished', shown)
        self.assertIn('不新建任务', shown)
        self.assertNotIn('compact task', rows[0][1])
        self.assertEqual(sum(m['role'] == 'tool' for m in client.sent[-1]), 5)
        self.assertEqual(client.sent[-1][-1]['content'].split('\n')[0], '[TASK_RESUME]')

    def test_english_resume_keywords_reach_the_unfinished_task(self):
        for text in ('continue', ' ReSuMe '):
            with self.subTest(text=text):
                shown, rows, client = self.run_cli(['update hermes', text],
                                                  [{'content': 'finished [TASK_DONE]'}], self.settings)
                self.assertEqual(len(rows), 1, rows)
                self.assertIn('finished', shown)
                self.assertIn('不新建任务', shown)
                self.assertNotIn('裸输入', shown)
                self.assertEqual(client.sent[-1][-1]['content'].split('\n')[0], '[TASK_RESUME]')

    def test_quoted_resume_keyword_is_literal_even_with_an_unfinished_task(self):
        shown, rows, client = self.run_cli(['update hermes', '"continue"'],
                                          [{'content': 'literal received [TASK_DONE]'}], self.settings)
        self.assertEqual(client.sent[-1][-1]['content'], '"continue"')
        self.assertIn('literal received', shown)
        self.assertNotIn('不新建任务', shown)
        self.assertEqual(len(rows), 1, rows)
        self.assertIn('已压缩上下文', shown)

    def test_a_new_instruction_reduces_old_tool_history_within_same_session(self):
        shown, rows, client = self.run_cli(['update hermes', 'now clean up the temp files'],
                                           [{'content': 'cleaned [TASK_DONE]'}], self.settings)
        self.assertIn('cleaned', shown)
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0][1], 'update hermes')
        self.assertIn('已压缩上下文', shown)
        self.assertLess(sum(m['role'] == 'tool' for m in client.sent[-1]), 5)

    def test_automatic_resume_finishes_the_whole_task_without_typing_anything(self):
        shown, rows, client = self.run_cli(
            ['update hermes'], [{'content': 'finished [TASK_DONE]'}],
            self.settings.replace('auto_resume = false', 'auto_resume = true'))
        self.assertIn('自动续跑 1/8：step_limit', shown)
        self.assertIn('finished', shown)
        self.assertEqual(len(rows), 1, rows)


class RetentionTests(Base):
    def reduce(self, groups, size, **changes):
        agent = self.build([{'content': 'x'}], auto_compact=True, **changes)
        agent.messages = [{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': 'goal'}]
        for index in range(groups):
            agent.messages.extend(make_group(index, size))
        self.assertTrue(agent._compact_for_context(rules=''))
        return agent

    def test_defaults_keep_more_recent_work(self):
        config = Config()
        self.assertEqual((config.auto_compact_keep_groups, config.tool_history_keep_groups), (6, 6))

    def test_deepest_retention_that_reaches_the_target_wins(self):
        kept = lambda agent: sum(m['role'] == 'tool' for m in agent.messages)
        # Six large groups would still leave the context above the target: fall back to three.
        self.assertEqual(kept(self.reduce(8, 8000, max_context_chars=70_000)), 3)
        # Six groups fit comfortably: all six are kept rather than the old two.
        self.assertEqual(kept(self.reduce(20, 3000, max_context_chars=80_000)), 6)
        self.assertEqual(kept(self.reduce(20, 3000, max_context_chars=80_000, auto_compact_keep_groups=2)), 2)

    def test_candidates_shrink_from_configured_depth_to_one_group(self):
        seen = []
        def spy(messages, keep, *args, **kwargs):
            seen.append(keep)
            return list(messages), {}
        for base, expected in ((6, [6, 3, 1] * 2), (2, [2, 1] * 2), (1, [1] * 2)):
            seen.clear()
            agent = self.build([{'content': 'x'}], auto_compact=True, max_context_chars=2000,
                               auto_compact_keep_groups=base)
            agent.messages = [{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': 'g' * 3000}]
            with patch('termux_agent.agent.compact_context_messages', side_effect=spy):
                agent._compact_for_context(rules='')
            self.assertEqual(seen, expected)


class ConfigAndStateTests(unittest.TestCase):
    def test_new_settings_are_validated(self):
        for key, value in (('auto_resume', 1), ('auto_resume_max', -1), ('auto_resume_max', 51),
                           ('auto_resume_max', True), ('auto_resume_max_wait', -1),
                           ('auto_resume_max_wait', 3601), ('auto_resume_max_wait', float('nan'))):
            with self.assertRaises(ValueError, msg=(key, value)):
                Config(**{key: value}).validate()
        Config(auto_resume_max=0, auto_resume_max_wait=0).validate()

    def test_profile_tables_can_set_them_and_top_level_keys_do_not_leak_between_profiles(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'config.toml'
            path.write_text('auto_resume_max = 3\n[apis.cloud]\nbase_url = "https://example.test/v1"\n'
                            'model = "m"\nauto_resume_max = 12\nauto_resume_max_wait = 90\n'
                            '[apis.plain]\nbase_url = "https://example.test/v1"\nmodel = "m"\n')
            self.assertEqual(Config.load(path, 'cloud').auto_resume_max, 12)
            self.assertEqual(Config.load(path, 'cloud').auto_resume_max_wait, 90)
            self.assertEqual(Config.load(path, 'plain').auto_resume_max, 3)
            self.assertEqual(Config.load(path, 'default').auto_resume_max, 3)

    def test_saved_state_with_damaged_new_fields_is_sanitized(self):
        state = validate_task_state({'id': 'a', 'goal': 'g', 'renewals': 'x', 'auto_resumes': -4,
                                     'budget_windows': 'oops'})
        self.assertEqual((state['renewals'], state['auto_resumes'], state['budget_windows']), (0, 0, []))
        state = validate_task_state({'id': 'a', 'budget_windows': [{'logical_calls': 3}, 'bad'] * 20})
        self.assertEqual(len(state['budget_windows']), 16)
        self.assertNotIn('renewals', validate_task_state({'id': 'a'}))

    def test_a_bare_stream_failure_event_says_so(self):
        from termux_agent.responses import _failure
        error = _failure({}, 'Responses 流式请求失败', event_type='response.failed')
        self.assertIn('response.failed', str(error))
        detailed = _failure({'error': {'code': 'server_error', 'message': 'boom'}}, 'Responses 流式请求失败',
                            event_type='response.failed')
        self.assertIn('server_error', str(detailed))
        self.assertNotIn('上游未提供', str(detailed))


if __name__ == '__main__':
    unittest.main()
