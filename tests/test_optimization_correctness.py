"""Cross-feature regressions for the optimization review."""
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from termux_agent import cli
from termux_agent.agent import Agent
from termux_agent.client import Client, IncompleteOutputError
from termux_agent.config import Config
from termux_agent.context import compact_messages, compact_task_messages
from termux_agent.continuation import control_prompt
from termux_agent.sessions import SessionStore, restore, snapshot, validate_messages
from termux_agent.token_budget import TokenCounter
from test_subagents import ScriptClient, call


def group(number):
    return [call('read_file', {'path': f'file-{number}'}, str(number)),
            {'role': 'tool', 'tool_call_id': str(number),
             'content': json.dumps({'content': 'source ' * 2000})}]


def manifest(messages):
    text = next(m['content'] for m in messages if str(m.get('content', '')).startswith('[Task memo: '))
    return json.loads(text[len('[Task memo: '):-1])['artifact_manifest']


class OptimizationCorrectnessTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        home = patch.dict(os.environ, {'HOME': str(self.root)})
        home.start(); self.addCleanup(home.stop)

    def make(self, client=None, **changes):
        a = Agent(Config(token_count_mode='off', approval_policy='never', **changes),
                  client=client or ScriptClient(), base_system='rules')
        self.addCleanup(a.close)
        return a

    def test_unmarked_existing_prompt_is_marked_by_controller(self):
        folder = self.root / '.config/termux-agent'
        folder.mkdir(parents=True)
        (folder / 'task_continue.txt').write_text('custom continuation without marker')
        a = self.make(ScriptClient({'content': 'working'}, {'content': 'done [TASK_DONE]'}), auto_continue=True)
        self.assertEqual(a.run('original goal'), 'done')
        nudge = a.messages[-2]['content']
        self.assertEqual(nudge, '[TASK_CONTINUE]\ncustom continuation without marker')
        self.assertEqual((folder / 'task_continue.txt').read_text(), 'custom continuation without marker')

    def test_resume_marker_is_not_duplicated_even_in_old_custom_template(self):
        self.assertEqual(control_prompt('[TASK_RESUME]', '[TASK_RESUME]\n[TASK_RESUME]\nresume'),
                         '[TASK_RESUME]\nresume')

    def test_compaction_after_nudge_retains_original_goal_and_pairs(self):
        messages = [{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': 'original goal'},
                    *group(0), {'role': 'assistant', 'content': 'working'},
                    {'role': 'user', 'content': control_prompt('[TASK_CONTINUE]', 'continue')},
                    *group(1), *group(2)]
        before = copy.deepcopy(messages)
        result, details = compact_task_messages(messages, 1, 'archive')
        self.assertIn({'role': 'user', 'content': 'original goal'}, result)
        self.assertEqual(details['omitted_groups'], 2)
        self.assertEqual(messages, before)
        validate_messages(result)

    def test_repeated_compaction_carries_bounded_prior_references(self):
        messages = [{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': 'goal'},
                    *group(0), *group(1), *group(2)]
        first, _ = compact_task_messages(messages, 1, 'archive-one')
        second, details = compact_task_messages([*first, *group(3), *group(4)], 1, 'archive-two')
        self.assertEqual({m['path'] for m in manifest(second)}, {f'file-{i}' for i in range(4)})
        self.assertEqual(details['reduced_calls'], 2)
        for i in range(5, 40):
            second, _ = compact_task_messages([*second, *group(i)], 1, f'archive-{i}')
        memo = next(m['content'] for m in second if m.get('content', '').startswith('[Task memo: '))
        self.assertLess(len(memo), 10000)
        self.assertLessEqual(len(manifest(second)), 24)

    def test_manual_turn_compaction_counts_humans_not_controller_nudges(self):
        messages = [{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': 'older task'},
                    {'role': 'assistant', 'content': 'older answer'}, {'role': 'user', 'content': 'current goal'},
                    *group(1), {'role': 'assistant', 'content': 'working'},
                    {'role': 'user', 'content': '[TASK_CONTINUE]\ncontinue'}, *group(2)]
        result, omitted = compact_messages(messages, 1, 'archive')
        self.assertGreater(omitted, 0)
        self.assertIn({'role': 'user', 'content': 'current goal'}, result)
        self.assertNotIn({'role': 'user', 'content': 'older task'}, result)
        validate_messages(result)

    def test_worker_limit_resets_on_new_human_task(self):
        parent = ScriptClient(call('delegate', {'task': 'part'}, 'd1'), {'content': 'done'},
                              call('delegate', {'task': 'part'}, 'd2'), {'content': 'done'})
        a = self.make(parent, max_subagents=1)
        with patch('termux_agent.agent.Client', side_effect=[ScriptClient({'content': 'one'}),
                                                           ScriptClient({'content': 'two'})]) as factory:
            a.run('first task'); first_id = a.task_state['id']
            a.run('second task')
        self.assertEqual(factory.call_count, 2)
        self.assertNotEqual(a.task_state['id'], first_id)
        self.assertEqual(a.task_state['subagents_started'], 1)

    def test_restore_and_continue_keep_worker_reservation(self):
        path = self.root / 'config.toml'
        path.write_text('approval_policy="never"\nmax_subagents=1\ntoken_count_mode="off"\n')
        a = Agent(Config.load(path), client=ScriptClient(call('delegate', {'task': 'part'}), {'content': 'done'}),
                  base_system='rules')
        self.addCleanup(a.close)
        with patch('termux_agent.agent.Client', return_value=ScriptClient({'content': 'finding'})):
            a.run('original task')
        scope = {'config_path': str(path), 'directory': str(self.root), 'instructions': '',
                 'session_file': None, 'disabled': True, 'instruction_path': None}
        payload = snapshot(a, scope); before = copy.deepcopy(payload)
        parent = ScriptClient(call('delegate', {'task': 'another'}, 'd2'), {'content': 'done'})
        def build(config, instructions, base_system=None):
            return Agent(config, client=parent, base_system=base_system)
        _, resumed, _ = restore(payload, build)
        self.addCleanup(resumed.close)
        with patch('termux_agent.agent.Client') as factory:
            resumed.continue_task()
        factory.assert_not_called()
        self.assertEqual(resumed.task_state['subagents_started'], 1)
        self.assertEqual(resumed.task_state['goal'], 'original task')
        self.assertEqual(payload, before)
        self.assertIn('limit reached', parent.requests[-1][0][-1]['content'])

    def test_worker_reservation_failure_stops_before_opening_client(self):
        a = self.make(ScriptClient(call('delegate', {'task': 'part'})))
        def save():
            if (a.task_state or {}).get('subagents_started'):
                raise OSError('reservation unavailable')
        a.checkpoint = save
        with patch('termux_agent.agent.Client') as factory, self.assertRaisesRegex(OSError, 'reservation unavailable'):
            a.run('task')
        factory.assert_not_called()
        self.assertEqual(a.last_run['status'], 'interrupted')

    def test_explicit_sub_starts_its_own_task(self):
        a = self.make(max_subagents=1)
        with patch('termux_agent.agent.Client', side_effect=[ScriptClient({'content': 'one'}),
                                                           ScriptClient({'content': 'two'})]):
            a.delegate('first'); previous = a.task_state['id']
            a.delegate('second')
        self.assertNotEqual(a.task_state['id'], previous)
        self.assertEqual(a.task_state['goal'], '/sub second')

    def test_bad_approval_values_are_configuration_errors(self):
        for value in ([], {}, None, True, 2, 'unknown'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'approval_policy'):
                Config(approval_policy=value).validate()

    def test_responses_context_incomplete_keeps_typed_flag_and_no_executable_calls(self):
        item = {'type': 'function_call', 'call_id': 'x', 'name': 'shell', 'arguments': '{'}
        for stream in (False, True):
            body = {'status': 'incomplete', 'incomplete_details': {'reason': 'context_shift'}, 'output': [item]}
            event = 'data: ' + json.dumps({'type': 'response.incomplete', 'response': body}) + '\n\n'
            def respond(request):
                return httpx.Response(200, text=event) if stream else httpx.Response(200, json=body)
            with self.subTest(stream=stream), httpx.Client(transport=httpx.MockTransport(respond)) as http:
                client = Client(Config(wire_api='responses', stream=stream), http_client=http)
                with self.assertRaises(IncompleteOutputError) as caught:
                    client.chat([], [])
                self.assertTrue(caught.exception.context_limited)
                self.assertNotIn('tool_calls', caught.exception.assistant_message)

    def test_preaccept_deadline_does_not_shorten_body_idle_timeout(self):
        seen = []
        def respond(request):
            seen.append(request.extensions['timeout'])
            return httpx.Response(200, json={'choices': [{'message': {'content': 'ok'}}]})
        with httpx.Client(transport=httpx.MockTransport(respond)) as http:
            client = Client(Config(timeout=120, retry_total_timeout=5, stream=False), http_client=http, clock=lambda: 100)
            client.chat([], [])
        self.assertEqual(seen[0]['read'], 120)
        self.assertEqual(seen[0]['connect'], 5)

    def test_debug_request_remains_independent_of_history(self):
        messages = [{'role': 'user', 'content': 'original'}]
        with httpx.Client(transport=httpx.MockTransport(lambda q: httpx.Response(
                200, json={'choices': [{'message': {'content': 'ok'}}]}))) as http:
            client = Client(Config(stream=False), http_client=http)
            client.chat(messages, [])
            messages[0]['content'] = 'mutated'
            self.assertEqual(client.last_request['messages'][0]['content'], 'original')
            client.last_request = None
            self.assertIsNone(client.last_request)

    def test_continue_reuses_one_measurement_for_an_unchanged_step(self):
        a = self.make(ScriptClient({'content': 'done'}))
        a.messages.extend([{'role': 'user', 'content': 'goal'}, {'role': 'assistant', 'content': 'previous'}])
        a._ensure_task_state('goal')
        with patch.object(a.token_counter, 'measure', wraps=a.token_counter.measure) as measure:
            a.continue_task()
        self.assertEqual(measure.call_count, 1)

    def test_checkpoint_single_copy_is_isolated_redacted_and_keeps_unknown_pair(self):
        secret = 'TEST_OPTIMIZATION_SECRET'
        a = self.make(api_key=secret)
        a._ensure_task_state('original goal')
        a.messages.extend([{'role': 'user', 'content': 'goal'},
                           call('write_file', {'path': 'note.txt', 'content': secret}, 'write')])
        before = copy.deepcopy(a.messages)
        scope = {'instructions': 'original'}; branch = {'kind': 'active'}
        payload = snapshot(a, scope, checkpoint=True, branch=branch)
        self.assertNotIn(secret, json.dumps(payload))
        self.assertIn('outcome is unknown', payload['messages'][-1]['content'])
        validate_messages(payload['messages'])
        payload['task_state']['goal'] = 'mutated'
        payload['scope']['instructions'] = 'mutated'
        payload['branch']['kind'] = 'mutated'
        payload['messages'][0]['content'] = 'mutated'
        self.assertEqual(a.messages, before)
        self.assertEqual(a.task_state['goal'], 'original goal')
        self.assertEqual(scope['instructions'], 'original')
        self.assertEqual(branch['kind'], 'active')

    def test_in_memory_archive_limit_stops_without_dropping_existing_archives(self):
        a = self.make()
        a.in_memory_archives = [{'id': str(i), 'messages': [{'role': 'system', 'content': 'old'}]} for i in range(16)]
        before = copy.deepcopy(a.in_memory_archives)
        with self.assertRaisesRegex(ValueError, 'archive limit'):
            a._publish_compaction(a.messages, a.messages, {}, 'new')
        self.assertEqual(a.in_memory_archives, before)

    def test_cli_unchanged_exit_skips_duplicate_revision_and_snapshot_is_single(self):
        path = self.root / 'config.toml'; path.write_text('token_count_mode="off"\n')
        saved, snapshots = [], []
        original_save = SessionStore.save
        def save(store, identifier, revision, payload, **kwargs):
            saved.append(copy.deepcopy(payload))
            return original_save(store, identifier, revision, payload, **kwargs)
        def take(*args, **kwargs):
            snapshots.append(kwargs)
            return snapshot(*args, **kwargs)
        previous = Path.cwd()
        try:
            with patch.object(cli.sys, 'argv', ['ta', '--config', str(path), '--directory', str(self.root), '--no-agent']), \
                    patch.object(cli.sys, 'stdout', io.StringIO()), patch('builtins.input', side_effect=['test', '/exit']), \
                    patch('termux_agent.agent.Client', return_value=ScriptClient({'content': 'done'})), \
                    patch.object(SessionStore, 'save', save), patch.object(cli, 'snapshot', side_effect=take):
                cli.main()
        finally:
            os.chdir(previous)
        # The successful finish checkpoint is already the final snapshot. The
        # ordinary save after the reply, /exit and finally must add no revisions.
        self.assertEqual(len([p for p in saved if not p.get('checkpoint')]), 0)
        self.assertGreaterEqual(len([p for p in saved if p.get('checkpoint')]), 3)
        self.assertEqual(sum(bool(k.get('checkpoint')) for k in snapshots),
                         sum(bool(p.get('checkpoint')) for p in saved))

    def test_all_new_session_commands_close_token_counters(self):
        path = self.root / 'config.toml'; path.write_text('token_count_mode="off"\n')
        closed = []
        previous = Path.cwd()
        try:
            with patch.object(cli.sys, 'argv', ['ta', '--config', str(path), '--directory', str(self.root), '--no-agent']), \
                    patch.object(cli.sys, 'stdout', io.StringIO()), \
                    patch('builtins.input', side_effect=['test', '/agent off', '/session new', '/clear', '/exit']), \
                    patch('termux_agent.agent.Client', side_effect=lambda config: ScriptClient({'content': 'done'})), \
                    patch.object(TokenCounter, 'close', autospec=True, side_effect=lambda counter: closed.append(counter)):
                cli.main()
        finally:
            os.chdir(previous)
        self.assertEqual(len(closed), 4)
        self.assertEqual(len({id(counter) for counter in closed}), 4)
