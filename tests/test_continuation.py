"""Bounded automatic continuation must preserve tool protocol and approvals."""
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from termux_agent.agent import Agent
from termux_agent.config import Config
from termux_agent.client import Client
from termux_agent.continuation import MarkerFilter, TASK_MARKERS
from termux_agent.sessions import validate_messages


def call(identifier, name, args):
    return {'id': identifier, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}


class ScriptedClient:
    last_usage = None
    last_request = None
    def __init__(self, replies): self.replies = iter(replies); self.sent = []
    def close(self): pass
    def chat(self, messages, tools):
        self.last_request = {'messages': copy.deepcopy(messages), 'tools': tools}
        self.sent.append(copy.deepcopy(messages))
        return next(self.replies)


class ContinuationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = patch.dict(os.environ, {'HOME': self.tmp.name})
        self.home.start()
    def tearDown(self):
        self.home.stop(); self.tmp.cleanup()

    def test_plan_only_pauses_resume_multiple_files_until_done(self):
        one = Path(self.tmp.name)/'one.md'; two = Path(self.tmp.name)/'two.md'
        one.write_text('one'); two.write_text('two')
        client = ScriptedClient([
            {'content': 'I will read the files.'},
            {'tool_calls': [call('1', 'read_file', {'path': str(one)})]},
            {'content': 'Next I will read the second file. [TASK_CONTINUE]'},
            {'tool_calls': [call('2', 'read_file', {'path': str(two)})]},
            {'content': 'Read both. [TASK_DONE]', 'reasoning_content': 'kept'},
        ])
        agent = Agent(Config(auto_continue=True, max_steps=8), client=client)
        original_system = agent.system
        self.assertEqual(agent.run('Read both files, continue without asking.'), 'Read both.')
        self.assertEqual({k: agent.last_run[k] for k in ('status', 'auto', 'continuations')},
                         {'status': 'complete', 'auto': True, 'continuations': 2})
        self.assertEqual(agent.stats()['api_calls_total'], 5)
        self.assertEqual(agent.stats()['tool_calls_total'], 2)
        self.assertEqual(agent.messages[0]['content'], original_system)
        self.assertIn('[TASK_DONE]', client.sent[0][0]['content'])
        self.assertEqual(agent.messages[-1]['reasoning_content'], 'kept')
        validate_messages(agent.messages)

    def test_normal_chat_still_finishes_in_one_request(self):
        client = ScriptedClient([{'content': 'hello'}])
        agent = Agent(Config(), client=client)
        self.assertEqual(agent.run('hello'), 'hello')
        self.assertEqual(len(client.sent), 1)
        self.assertFalse((Path(self.tmp.name)/'.config/termux-agent/task_prompt.txt').exists())

    def test_no_tool_spin_is_bounded_and_blocked_status_stops(self):
        client = ScriptedClient([{'content': 'I will continue.'}] * 10)
        agent = Agent(Config(auto_continue=True, max_steps=8, max_continuations=2), client=client)
        self.assertIn('自动任务已停止', agent.run('do the task'))
        self.assertEqual(len(client.sent), 3)
        self.assertEqual(agent.last_run['status'], 'stalled')
        client = ScriptedClient([{'content': 'Need the target path. [TASK_BLOCKED]'}])
        agent = Agent(Config(auto_continue=True), client=client)
        self.assertEqual(agent.run('do task'), 'Need the target path.')
        self.assertEqual(len(client.sent), 1)
        self.assertEqual(agent.last_run['status'], 'blocked')

    def test_all_requests_share_max_steps_and_denied_tools_are_not_replayed(self):
        client = ScriptedClient([{'content': 'working [TASK_CONTINUE]'}] * 5)
        agent = Agent(Config(auto_continue=True, max_steps=2), client=client)
        self.assertIn('最大模型请求', agent.run('task'))
        self.assertEqual(len(client.sent), 2)
        target = Path(self.tmp.name)/'file'
        client = ScriptedClient([
            {'tool_calls': [call('1', 'write_file', {'path': str(target), 'content': 'deny'})]},
            {'tool_calls': [call('2', 'write_file', {'path': str(target), 'content': 'still deny'})]},
        ])
        approve = []
        agent = Agent(Config(auto_continue=True, approval_policy='always'), client=client,
                      approve=lambda name,args: (approve.append(name) or False))
        self.assertIn('审批拒绝', agent.run('task'))
        self.assertFalse(target.exists())
        self.assertEqual(approve, ['write_file'])
        self.assertEqual(agent.last_run['status'], 'blocked')
        validate_messages(agent.messages)

    def test_control_markers_are_filtered_across_stream_chunks(self):
        filter = MarkerFilter(TASK_MARKERS)
        parts = ['Finished [TA', 'SK_D', 'ONE]', '\n']
        self.assertEqual(''.join(filter.feed(x) for x in parts) + filter.feed('', final=True), 'Finished \n')
        frames = [f'data: {json.dumps({"choices":[{"delta":{"content":x}}]})}\n\n' for x in parts]
        def respond(request):
            return httpx.Response(200, content=''.join(frames), headers={'content-type':'text/event-stream'})
        with httpx.Client(transport=httpx.MockTransport(respond)) as http:
            client = Client(Config(), http)
            client.set_control_markers(TASK_MARKERS)
            displayed = []; client.on_stream = lambda kind,value: displayed.append(value)
            answer = client.chat([], [])
            self.assertIn('[TASK_DONE]', answer['content'])
            self.assertNotIn('[TASK_', ''.join(displayed))

    def test_cli_one_read_grant_covers_only_same_automatic_task(self):
        from termux_agent import cli
        root = Path(self.tmp.name)
        one=root/'one.md'; two=root/'two.md'; one.write_text('one'); two.write_text('two')
        config=root/'config.toml'; config.write_text('approval_policy="always"\n')
        clients=[]
        class FakeClient(ScriptedClient):
            def __init__(self, cfg):
                super().__init__([
                    {'tool_calls':[call('1','read_file',{'path':str(one)})]},
                    {'tool_calls':[call('2','read_file',{'path':str(two)})]},
                    {'content':'Read both. [TASK_DONE]'},
                ]); clients.append(self)
        previous=Path.cwd()
        try:
            with patch('termux_agent.agent.Client', FakeClient), \
                 patch.object(cli.sys,'argv',['ta','--no-session','--no-agent','--config',str(config),'--directory',str(root)]), \
                 patch.object(cli.sys.stdin,'isatty',return_value=True), \
                 patch('builtins.input',side_effect=['/run Read both files','a','/exit']) as inputs, \
                 patch('sys.stdout',new_callable=io.StringIO) as output:
                cli.main()
                self.assertEqual(inputs.call_count,3)
                self.assertIn('Read both.',output.getvalue())
        finally: os.chdir(previous)
        results=[m for m in clients[0].sent[-1] if m['role']=='tool']
        self.assertEqual(len(results),2)
        self.assertTrue(all('error' not in json.loads(m['content']) for m in results))

    def test_provider_refusal_is_a_blocker_not_a_continuation_trigger(self):
        for reply in ({'content':'The request was rejected because it was considered high risk'},
                      {'content':'', 'refusal':'provider refused'}):
            client = ScriptedClient([reply])
            agent = Agent(Config(auto_continue=True), client=client)
            agent.run('read the files')
            self.assertEqual(len(client.sent), 1)
            self.assertEqual(agent.last_run['status'], 'blocked')
            self.assertEqual(agent.last_run['blocked_by'], 'provider')
            self.assertEqual(agent.last_run['continuations'], 0)

    def test_content_filter_finish_reason_stops_without_retry(self):
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(200,json={'choices':[{'message':{'content':'filtered'},'finish_reason':'content_filter'}]})
        with httpx.Client(transport=httpx.MockTransport(respond)) as http:
            client = Client(Config(stream=False), http)
            agent = Agent(Config(auto_continue=True), client=client)
            self.assertEqual(agent.run('task'), 'filtered')
            self.assertEqual(agent.last_run['status'], 'blocked')
            self.assertEqual(len(requests), 1)
