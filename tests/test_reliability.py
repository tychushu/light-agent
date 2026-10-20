"""Long contexts, process death, recovery and local archive controls."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx

from termux_agent.agent import Agent
from termux_agent.client import Client, ClientError, ContextLengthError, EmptyResponseError
from termux_agent.config import Config
from termux_agent.context import compact_messages, context_chars
from termux_agent.sessions import SessionStore, snapshot, validate_messages


class Replies:
    last_usage = None
    def __init__(self, *answers):
        self.answers = iter(answers)
        self.calls = 0
    def chat(self, *args):
        self.calls += 1
        return next(self.answers)
    def close(self):
        pass


class ReliabilityTests(unittest.TestCase):
    def test_character_budget_blocks_before_network_without_dropping_history(self):
        client = Replies({'content': 'should not run'})
        agent = Agent(Config(max_context_chars=1000), client=client)
        before = copy.deepcopy(agent.messages)
        self.assertIn('/compact', agent.run('keep this request'))
        self.assertEqual(client.calls, 0)
        self.assertEqual(agent.messages[:-1], before)
        self.assertEqual(agent.messages[-1]['content'], 'keep this request')
        self.assertEqual(agent.stats()['api_calls_total'], 0)

    def test_eighty_percent_warning_includes_tool_schema_and_reasoning_usage(self):
        client = Replies({'content': 'ok'})
        client.last_usage = {'completion_tokens': 12, 'completion_tokens_details': {'reasoning_tokens': 9}}
        events = []
        agent = Agent(Config(), client=client, event=lambda k, p: events.append(k))
        size = context_chars([*agent.messages, {'role': 'user', 'content': 'hi'}], agent.tools)
        agent.config.max_context_chars = int(size / 0.9)
        self.assertEqual(agent.run('hi'), 'ok')
        self.assertIn('context_warning', events)
        self.assertEqual(agent.stats()['usage_totals']['reasoning_tokens'], 9)

    def test_context_errors_are_classified_without_retry_or_body_echo(self):
        for error in ({'code': 'context_length_exceeded', 'message': 'PRIVATE'},
                      {'message': 'Maximum context length is 1000 PRIVATE'},
                      {'code': 'prompt_too_long'}):
            with self.subTest(error=error):
                seen = []
                def respond(request):
                    seen.append(request)
                    return httpx.Response(400, json={'error': error})
                with httpx.Client(transport=httpx.MockTransport(respond)) as http:
                    with self.assertRaises(ContextLengthError) as caught:
                        Client(Config(stream=False, retry_thinking_400=True), http_client=http).chat([], [])
                self.assertEqual(len(seen), 1)
                self.assertNotIn('PRIVATE', str(caught.exception))

    def test_unrelated_400_and_waf_remain_generic(self):
        for response in (httpx.Response(400, json={'error': {'message': 'bad parameter'}}),
                         httpx.Response(405, text='<html>maximum context length</html>', headers={'content-type': 'text/html'})):
            with httpx.Client(transport=httpx.MockTransport(lambda r: response)) as http:
                with self.assertRaises(ClientError) as caught:
                    Client(Config(stream=False), http_client=http).chat([], [])
                self.assertNotIsInstance(caught.exception, ContextLengthError)

    def test_empty_reasoning_answer_keeps_usage_and_never_retries_json_or_sse(self):
        usage = {'completion_tokens': 10, 'completion_tokens_details': {'reasoning_tokens': 10}}
        for stream in (False, True):
            calls = []
            def respond(request):
                calls.append(request)
                if stream:
                    events = [{'choices': [{'delta': {'reasoning_content': 'thinking'}}]},
                              {'choices': [{'delta': {}, 'finish_reason': 'length'}], 'usage': usage}]
                    return httpx.Response(200, text=''.join('data: '+json.dumps(e)+'\n\n' for e in events),
                                          headers={'content-type': 'text/event-stream'})
                return httpx.Response(200, json={'choices': [{'message': {'content': '', 'reasoning_content': 'thinking'},
                                                             'finish_reason': 'length'}], 'usage': usage})
            with httpx.Client(transport=httpx.MockTransport(respond)) as http:
                client = Client(Config(stream=stream), http_client=http)
                agent = Agent(client.config, client=client)
                self.assertIn('输出预算', agent.run('hello'))
                self.assertEqual(agent.last_run['status'], 'empty_response')
                self.assertEqual(agent.stats()['usage_totals']['reasoning_tokens'], 10)
                self.assertEqual(agent.messages[-1]['reasoning_content'], 'thinking')
            self.assertEqual(len(calls), 1)

    def test_compaction_keeps_whole_tool_turns_and_original_is_untouched(self):
        messages = [{'role': 'system', 'content': 'rules'}]
        for i in range(4):
            messages.extend([{'role': 'user', 'content': str(i)},
                             {'role': 'assistant', 'content': '', 'reasoning_content': 'signed thinking',
                              'tool_calls': [{'id': str(i), 'function': {'name': 'shell', 'arguments': '{}'}}]},
                             {'role': 'tool', 'tool_call_id': str(i), 'content': 'ok'},
                             {'role': 'assistant', 'content': 'done'}])
        original = copy.deepcopy(messages)
        result, omitted = compact_messages(messages, 2, 'abcdef123456')
        self.assertEqual(omitted, 8)
        self.assertEqual(result[2:], messages[9:])
        self.assertEqual(messages, original)
        self.assertIn('abcdef123456', result[1]['content'])
        validate_messages(result)
        second, _ = compact_messages(result, 1, '123456abcdef')
        validate_messages(second)
        self.assertEqual(second[2:], messages[13:])
        for invalid in (0, -1, True, 101):
            with self.assertRaises(ValueError):
                compact_messages(messages, invalid, 'x')

    def test_tool_checkpoints_are_recoverable_without_mutating_live_calls(self):
        calls = [{'id': str(i), 'type': 'function', 'function': {'name': 'shell', 'arguments': '{}'}} for i in range(2)]
        agent = Agent(Config(approval_policy='never'), client=Replies({'tool_calls': calls}, {'content': 'done'}))
        checkpoints = []
        agent.checkpoint = lambda: checkpoints.append(snapshot(agent, {}, checkpoint=True))
        with patch('termux_agent.agent.execute', return_value={'ok': True}) as execute:
            agent.run('run tools')
        self.assertEqual(execute.call_count, 2)
        for saved in checkpoints:
            validate_messages(saved['messages'])
        pending = next(p for p in checkpoints if any('unknown' in m.get('content', '') for m in p['messages']))
        self.assertTrue(pending['checkpoint'])
        self.assertFalse(any('unknown' in m.get('content', '') for m in agent.messages))

    def test_checkpoint_write_failure_stops_before_model_or_tools(self):
        client = Replies({'content': 'unused'})
        agent = Agent(Config(), client=client)
        def fail():
            raise OSError('disk full')
        agent.checkpoint = fail
        with self.assertRaises(OSError):
            agent.run('task')
        self.assertEqual(client.calls, 0)

    def test_wal_survives_process_death_after_side_effect_before_result(self):
        with tempfile.TemporaryDirectory() as td:
            script = '''
import os
from pathlib import Path
from termux_agent.agent import Agent
from termux_agent.config import Config
from termux_agent.sessions import SessionStore,snapshot
import termux_agent.agent as core
root=Path(os.environ['RELIABILITY_ROOT'])
store=SessionStore(root)
class Client:
 def chat(self,*a): return {'tool_calls':[{'id':'call','type':'function','function':{'name':'shell','arguments':'{}'}}]}
agent=Agent(Config(approval_policy='never'),client=Client())
revision=0
def checkpoint():
 global revision
 revision=store.save('abcdef123456',revision,snapshot(agent,{},checkpoint=True))
def execute(*a):
 store.save_draft('abcdef123456',revision,{'content':'partial stream'})
 (root/'effect').write_text('executed')
 os._exit(9)
agent.checkpoint=checkpoint
core.execute=execute
agent.run('do task')
'''
            run = subprocess.run([sys.executable, '-c', script], env=dict(os.environ, RELIABILITY_ROOT=td),
                                 capture_output=True, timeout=15)
            self.assertEqual(run.returncode, 9, run.stderr.decode())
            self.assertTrue((Path(td)/'effect').exists())
            store = SessionStore(td)
            try:
                _, payload = store.load('abcdef123456')
                validate_messages(payload['messages'])
                self.assertIn('outcome is unknown', payload['messages'][-1]['content'])
                self.assertEqual(store.draft('abcdef123456')['content'], 'partial stream')
                self.assertEqual(store.db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                self.assertEqual(store.db.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
                self.assertEqual(store.db.execute('PRAGMA synchronous').fetchone()[0], 2)
                for suffix in ('', '-wal', '-shm'):
                    p = Path(td)/('sessions.sqlite3'+suffix)
                    if p.exists(): self.assertEqual(p.stat().st_mode & 0o777, 0o600)
            finally:
                store.close()

    def test_cli_compact_recall_and_export_preserve_original_without_extra_calls(self):
        from termux_agent import cli
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = root/'config.toml'
            config.write_text('base_url="https://example.com/v1"\nmodel="test"\n')
            calls = []
            class Echo:
                def __init__(self, config): pass
                def chat(self, messages, tools):
                    calls.append(messages[-1]['content'])
                    return {'content': 'answer ' + messages[-1]['content']}
                def close(self): pass
            original = Path.cwd()
            args = ['ta', '--config', str(config), '--directory', td, '--no-agent']
            prompts = ['first', 'second keyword', 'third', '/compact 1', '/recall keyword',
                       '/export json "' + str(root/'saved export.json') + '"', '/exit']
            try:
                with patch.object(cli.sys, 'argv', args), patch('builtins.input', side_effect=prompts), \
                        patch('builtins.print'), patch('termux_agent.agent.Client', Echo), \
                        patch.object(cli, 'SessionStore', lambda: SessionStore(root/'store')):
                    cli.main()
            finally:
                os.chdir(original)
            store = SessionStore(root/'store')
            try:
                rows = store.listing()
                self.assertEqual(len(rows), 2)
                archive = next(r for r in rows if not r['title'].endswith(' · compact'))
                current = next(r for r in rows if r['title'].endswith(' · compact'))
                full = store.load(archive['id'])[1]['messages']
                compact = store.load(current['id'])[1]['messages']
                self.assertEqual(len(full), 7)
                self.assertEqual(len(compact), 4)
                self.assertIn(archive['id'], compact[1]['content'])
                self.assertEqual(compact[2]['content'], 'third')
                self.assertEqual(store.recall('keyword')[0]['id'], archive['id'])
                self.assertEqual(json.loads((root/'saved export.json').read_text())['id'], current['id'])
                self.assertEqual(calls, ['first', 'second keyword', 'third'])
            finally:
                store.close()

    def test_cli_partial_stream_draft_survives_error_exit_and_resume(self):
        from termux_agent import cli
        import io
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); config = root/'config.toml'
            config.write_text('base_url="https://example.com/v1"\nmodel="test"\n')
            class BrokenStream:
                def __init__(self, config): pass
                def chat(self, *args):
                    self.on_stream('content', 'unfinished visible reply')
                    raise RuntimeError('stream interrupted')
                def close(self): pass
            original = Path.cwd()
            def run(args, prompts):
                out = io.StringIO()
                try:
                    with patch.object(cli.sys, 'argv', ['ta', *args]), patch('builtins.input', side_effect=prompts), \
                            patch.object(cli.sys, 'stdout', out), patch('termux_agent.agent.Client', BrokenStream), \
                            patch.object(cli, 'SessionStore', lambda: SessionStore(root/'store')):
                        cli.main()
                finally:
                    os.chdir(original)
                return out.getvalue()
            run(['--config', str(config), '--directory', td, '--no-agent'], ['hello', '/exit'])
            store = SessionStore(root/'store')
            identifier = store.ids()[0]
            self.assertEqual(store.draft(identifier)['content'], 'unfinished visible reply')
            store.close()
            output = run(['--session', identifier], ['/title Recovered', '/exit'])
            self.assertIn('unfinished visible reply', output)
            store = SessionStore(root/'store')
            try:
                self.assertEqual(store.draft(identifier)['content'], 'unfinished visible reply')
                self.assertEqual([m['role'] for m in store.load(identifier)[1]['messages']], ['system', 'user'])
            finally:
                store.close()

    def test_context_budget_config_is_per_profile_and_validated(self):
        with tempfile.TemporaryDirectory() as td:
            config = Path(td)/'config.toml'
            config.write_text('max_context_chars=600000\n[apis.cloud]\nbase_url="https://example.com/v1"\n'
                              'model="x"\nmax_context_chars=123456\n')
            self.assertEqual(Config.load(config).max_context_chars, 600000)
            self.assertEqual(Config.load(config, 'cloud').max_context_chars, 123456)
            for value in (True, 0, '1000', 1000.5, 20_000_001):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    Config(max_context_chars=value).validate()

    def test_drafts_survive_metadata_saves_and_reject_stale_writers(self):
        with tempfile.TemporaryDirectory() as td:
            store = SessionStore(td)
            try:
                agent = Agent(Config(), client=Replies())
                payload = snapshot(agent, {})
                rev = store.save('abcdef123456', 0, payload)
                store.save_draft('abcdef123456', rev, {'content': 'partial'})
                rev = store.save('abcdef123456', rev, payload, clear_draft=False)
                self.assertEqual(store.draft('abcdef123456')['content'], 'partial')
                with self.assertRaises(ValueError):
                    store.save_draft('abcdef123456', rev-1, {'content': 'stale'})
                self.assertEqual(store.draft('abcdef123456')['content'], 'partial')
                store.save('abcdef123456', rev, payload)
                self.assertIsNone(store.draft('abcdef123456'))
            finally:
                store.close()

    def test_recall_literal_chinese_search_hides_write_payloads_and_export_is_exclusive(self):
        with tempfile.TemporaryDirectory() as td:
            store = SessionStore(td)
            try:
                agent = Agent(Config(), client=Replies())
                agent.messages += [{'role': 'user', 'content': '查询中文 100%_literal'},
                                   {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'x',
                                    'function': {'name': 'write_file', 'arguments': json.dumps({'path':'note.txt', 'content':'TOPSECRET'})}}]},
                                   {'role': 'tool', 'tool_call_id': 'x', 'content': '{}'}]
                store.save('abcdef123456', 0, snapshot(agent, {}))
                self.assertEqual(len(store.recall('中文')), 1)
                self.assertEqual(len(store.recall('100%_literal')), 1)
                self.assertNotIn('TOPSECRET', store.recall('TOPSECRET')[0]['snippet'])
                path = store.export('abcdef123456', 'json')
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(json.loads(path.read_text())['payload']['messages'], agent.messages)
                with self.assertRaises(FileExistsError):
                    store.export('abcdef123456', 'json', path)
                md = store.export('abcdef123456')
                self.assertIn('## 1 · user', md.read_text())
                self.assertIn('TOPSECRET', md.read_text())
                store.delete('abcdef123456')
                self.assertEqual(store.recall('中文'), [])
            finally:
                store.close()
