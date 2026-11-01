"""Bare REPL keywords guide users without entering task or command paths."""
import unittest
from unittest.mock import patch

import test_session_selection as fixtures
from termux_agent import cli, i18n
from termux_agent.agent import Agent


KEYWORDS = ('continue', 'clear', 'compact', 'status', 'help', 'exit', 'resume')
ENGLISH_HINT = ('Bare {v0} does not execute a command; use {v1} to run it. '
                'To send it as text, enter "{v0}" with quotes or use a complete sentence.')
FakeClient = fixtures.FakeClient


class TrackingAgent(Agent):
    instances = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.run_calls = []
        self.continue_calls = []
        self.stats_calls = 0
        self.instances.append(self)

    def run(self, text, auto_continue=None):
        self.run_calls.append((text, auto_continue))
        return super().run(text, auto_continue=auto_continue)

    def continue_task(self, steps=None, auto_continue=None):
        self.continue_calls.append(steps)
        return 'continued'

    def stats(self):
        self.stats_calls += 1
        return super().stats()


class CommandGuardTests(unittest.TestCase):
    # Reuse the lightweight fixtures without inheriting their TestCase methods.
    setUp = fixtures.SessionSelectionTests.setUp
    tearDown = fixtures.SessionSelectionTests.tearDown
    run_cli = fixtures.SessionSelectionTests.run_cli
    add_session = fixtures.SessionSelectionTests.add_session

    def run_guard(self, *args, **kwargs):
        TrackingAgent.instances.clear()
        with patch('termux_agent.cli.Agent', TrackingAgent):
            result = self.run_cli(*args, **kwargs)
        return result

    def test_seven_keywords_case_and_whitespace_keep_state_goal_and_history(self):
        inputs = []
        for keyword in KEYWORDS:
            inputs.extend((keyword, f' \t{keyword.upper()} '))
        inputs.append('/exit')

        output, prompts, _, _ = self.run_guard(inputs)

        agent = TrackingAgent.instances[-1]
        self.assertEqual(len(TrackingAgent.instances), 1)
        self.assertEqual(len(prompts), len(inputs))
        self.assertEqual(FakeClient.requests, [])
        self.assertEqual(agent.run_calls, [])
        self.assertEqual(agent.continue_calls, [])
        self.assertEqual(agent._total_calls, 0)
        self.assertIsNone(agent.task_state)
        self.assertEqual(agent.last_run, Agent._idle_run('zh'))
        self.assertEqual([message['role'] for message in agent.messages], ['system'])
        self.assertEqual(self.store.listing(), [])
        for keyword in KEYWORDS:
            with self.subTest(keyword=keyword):
                self.assertEqual(output.count(f'如需执行请使用 /{keyword}。'), 2)
                self.assertIn(f'带引号的 "{keyword}"', output)
        self.assertNotIn('Error:', output)

    def test_nontty_guard_returns_to_next_command_without_consuming_it(self):
        output, prompts, _, _ = self.run_guard([' \tReSuMe ', '/status', '/exit'])

        self.assertEqual(len(prompts), 3)
        self.assertEqual(output.count('如需执行请使用 /resume。'), 1)
        self.assertLess(output.index('如需执行请使用 /resume。'), output.index('尚无任务。'))
        self.assertEqual(TrackingAgent.instances[-1].stats_calls, 1)
        self.assertEqual(FakeClient.requests, [])

    def test_explicit_slash_continue_still_calls_continue_task(self):
        output, _, _, _ = self.run_guard(['/continue 2', '/exit'])

        self.assertEqual(TrackingAgent.instances[-1].continue_calls, [2])
        self.assertIn('continued', output)
        self.assertEqual(FakeClient.requests, [])

    def test_run_continue_is_an_explicit_task(self):
        TrackingAgent.instances.clear()
        with patch('termux_agent.cli.Agent', TrackingAgent), \
                patch.object(TrackingAgent, 'run', return_value='task accepted') as run:
            output, _, _, _ = self.run_cli(['/run continue', '/exit'])

        run.assert_called_once_with('continue', auto_continue=True)
        self.assertIn('task accepted', output)
        self.assertNotIn('如需执行请使用 /continue。', output)

    def test_literal_sentence_and_shell_inputs_remain_normal(self):
        with patch('termux_agent.tools.execute', return_value={'output': 'shell ok', 'exit_code': 0}) as execute:
            output, _, _, _ = self.run_guard(
                ['"continue"', 'continue with tests', '!continue', '/exit'], allow_chat=True)

        self.assertEqual([request[-1]['content'] for request in FakeClient.requests],
                         ['"continue"', 'continue with tests'])
        execute.assert_called_once_with(
            'shell', {'command': 'continue', 'cwd': str(self.root)}, 20_000)
        self.assertIn('shell ok', output)
        self.assertNotIn('如需执行请使用 /continue。', output)

    def test_file_submission_containing_only_continue_is_sent_verbatim(self):
        submission = self.root / 'continue.txt'
        submission.write_text('continue', encoding='utf-8')

        output, _, _, _ = self.run_guard([str(submission), '/exit'], allow_chat=True)

        self.assertEqual(FakeClient.requests[-1][-1]['content'], 'continue')
        self.assertNotIn('如需执行请使用 /continue。', output)

    def test_prompt_continue_is_explicit_single_prompt_api_input(self):
        output, prompts, _, _ = self.run_guard(
            [], args=('--prompt', 'continue'), allow_chat=True)

        self.assertEqual(prompts, [])
        self.assertEqual(FakeClient.requests[-1][-1]['content'], 'continue')
        self.assertNotIn('如需执行请使用 /continue。', output)

    def test_history_only_guard_precedes_rejection_and_history_stays_usable(self):
        self.add_session('aaaaaaaaaaaa', 'Archive', 1_900_000_000)
        before = self.store.load('aaaaaaaaaaaa')
        self.config.write_text(
            'base_url="https://new.example/v1"\nmodel="new-model"\napproval_policy="never"\n'
            'auto_continue=false\n')

        output, prompts, _, _ = self.run_guard(
            ['continue', '/history', '/exit'], args=('--session', 'aaaaaaaaaaaa'))

        self.assertEqual(len(prompts), 3)
        self.assertIn('如需执行请使用 /continue。', output)
        self.assertIn('archived answer', output)
        self.assertNotIn('当前为历史查看模式；请用 /api NAME 选择可用 API 并新建会话。', output)
        self.assertEqual(FakeClient.requests, [])
        self.assertEqual(self.store.load('aaaaaaaaaaaa'), before)

    def test_english_hint_contract_uses_the_stable_chinese_source_template(self):
        self.config.write_text('ui_language="en"\n' + self.config.read_text())
        formatted = ENGLISH_HINT.format(v0='continue', v1='/continue')

        self.assertEqual(i18n.EN[cli.BARE_COMMAND_HINT], ENGLISH_HINT)
        output, _, _, _ = self.run_guard(['continue', '/exit'])

        self.assertIn(formatted, output)
        self.assertNotIn('裸输入', output)


if __name__ == '__main__':
    unittest.main()
