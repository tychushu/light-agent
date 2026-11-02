"""Long-task budgets, durable cooldowns and bounded-stack segment extension."""
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

import test_mimo_acceptance as fixtures
from termux_agent.agent import Agent
from termux_agent.client import Client
from termux_agent.config import Config
from termux_agent.sessions import snapshot,restore


class LongTaskRetryTests(unittest.TestCase):
    def test_many_segments_complete_with_low_recursion_limit(self):
        code=r'''
import json,sys
from unittest.mock import patch
from termux_agent.agent import Agent
from termux_agent.config import Config
sys.setrecursionlimit(130)
class Reply:
    last_usage=None
    count=0
    def chat(self,*args):
        self.count+=1
        if self.count==128:return {'content':'done'}
        return {'tool_calls':[{'id':str(self.count),'type':'function','function':{'name':'read_file','arguments':'{"path":"x"}'}}]}
    def close(self):pass
client=Reply()
a=Agent(Config(max_steps=1,max_task_calls=130,auto_extend_segments=True,
               auto_compact=False,token_count_mode='off'),client=client,base_system='rules')
with patch('termux_agent.agent.execute',return_value={'content':'ok'}):result=a.run('task')
assert result=='done',result
assert client.count==128 and a.task_state['segments']==128
assert a._tool_calls_executed==127
print('128 bounded segments complete')
'''
        result=subprocess.run([sys.executable,'-c',code],cwd=fixtures.REPO_ROOT,text=True,
                              capture_output=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('128 bounded segments',result.stdout)

    def test_retry_wait_cannot_exceed_remaining_task_time(self):
        now=[100.];sent=[];sleeps=[]
        def response(request):
            sent.append(request)
            return httpx.Response(429,json={'error':{'code':'rate_limit_exceeded'}},headers={'Retry-After':'60'})
        cfg=Config(stream=False,max_task_seconds=1,retry_wait_budget=180,retry_total_timeout=300)
        with patch('termux_agent.agent.time.monotonic',side_effect=lambda:now[0]), \
             httpx.Client(transport=httpx.MockTransport(response)) as http:
            client=Client(cfg,http_client=http,sleeper=sleeps.append,clock=lambda:now[0],randomizer=lambda:0)
            a=Agent(cfg,client=client);a.run('task')
        self.assertEqual(len(sent),1)
        self.assertEqual(sleeps,[])
        self.assertEqual(a.last_run['stop_reason'],'rate_limit')

    def test_elapsed_header_time_exhausts_task_without_new_attempt(self):
        now=[100.];sent=[]
        def response(request):
            sent.append(request);now[0]+=1.1
            return httpx.Response(503)
        cfg=Config(stream=False,max_task_seconds=1,retry_wait_budget=180)
        with patch('termux_agent.agent.time.monotonic',side_effect=lambda:now[0]), \
             httpx.Client(transport=httpx.MockTransport(response)) as http:
            a=Agent(cfg,client=Client(cfg,http_client=http,clock=lambda:now[0],sleeper=lambda _:self.fail('wait')))
            a.run('task')
        self.assertEqual(len(sent),1)
        self.assertEqual(a.last_run['stop_reason'],'time_budget')

    def test_cooldown_survives_restore_without_changing_goal_or_replaying_tools(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);path=root/'config.toml'
            path.write_text('model="test"\nbase_url="https://example.test/v1"\nstream=false\n'
                            'retry_wait_budget=90\nretry_max_attempts=1\n')
            cfg=Config.load(path);now=[100.];wall=[1000.];sleeps=[];sent=[];checkpoints=[]
            def fail(request):return httpx.Response(429,json={'error':{'code':'rate_limit_exceeded'}},headers={'Retry-After':'30'})
            def success(request):sent.append((wall[0],request.content));return httpx.Response(200,json={'choices':[{'message':{'content':'done'}}]})
            def sleep(delay):sleeps.append(delay);now[0]+=delay;wall[0]+=delay
            with patch('termux_agent.agent.time.monotonic',side_effect=lambda:now[0]), \
                 patch('termux_agent.agent.time.time',side_effect=lambda:wall[0]), \
                 httpx.Client(transport=httpx.MockTransport(fail)) as first, \
                 httpx.Client(transport=httpx.MockTransport(success)) as second:
                a=Agent(cfg,client=Client(cfg,http_client=first,clock=lambda:now[0],wall_clock=lambda:wall[0],randomizer=lambda:0))
                a.run('keep original goal')
                self.assertEqual(a.last_run['stop_reason'],'rate_limit')
                scope={'directory':td,'config_path':str(path),'instructions':'','session_file':None,'disabled':True,'instruction_path':None}
                payload=snapshot(a,scope);original=copy.deepcopy(payload)
                def build(config,text,base_system=None):
                    return Agent(config,client=Client(config,http_client=second,sleeper=sleep,
                        clock=lambda:now[0],wall_clock=lambda:wall[0],randomizer=lambda:0),base_system=base_system)
                _,resumed,_=restore(payload,build)
                resumed.checkpoint=lambda:checkpoints.append(snapshot(resumed,scope,checkpoint=True))
                with patch('termux_agent.agent.execute',side_effect=AssertionError('tool replay')):
                    self.assertEqual(resumed.continue_task(),'done')
                self.assertEqual(resumed.task_state['goal'],'keep original goal')
                self.assertEqual(resumed.task_state['id'],a.task_state['id'])
                self.assertEqual(resumed.task_state['logical_calls'],2)
                self.assertEqual(resumed._conversation_count,1)
                self.assertNotIn('retry_not_before',resumed.task_state)
            self.assertEqual(payload,original)
            self.assertEqual(sleeps,[30])
            self.assertGreaterEqual(sent[0][0],1030)
            self.assertTrue(any(p['task_state'].get('retry',{}).get('phase')=='wait' for p in checkpoints))
            self.assertGreaterEqual(resumed.task_state['active_seconds'],30)

    def test_ctrl_c_during_backoff_keeps_durable_cooldown_and_no_more_requests(self):
        sent=[];saved=[]
        def response(request):
            sent.append(request)
            return httpx.Response(429,json={'error':{'type':'rate_limit_error'}},headers={'Retry-After':'10'})
        with httpx.Client(transport=httpx.MockTransport(response)) as http:
            client=Client(Config(stream=False),http_client=http,
                          sleeper=lambda _:(_ for _ in ()).throw(KeyboardInterrupt()),randomizer=lambda:0)
            a=Agent(client.config,client=client)
            a.checkpoint=lambda:saved.append(snapshot(a,{},checkpoint=True))
            a.run('original')
        self.assertEqual(len(sent),1)
        self.assertEqual(a.last_run['status'],'interrupted')
        self.assertIn('retry_not_before',saved[-1]['task_state'])
        self.assertEqual(saved[-1]['task_state']['goal'],'original')
        self.assertEqual(saved[-1]['counters']['_http_attempts_total'],1)
        self.assertEqual(saved[-1]['counters']['_http_retries_total'],0)

    def test_usage_missing_counts_include_inflight_requests_recovered_without_receipts(self):
        a=Agent(Config(token_count_mode='off'),client=fixtures.ScriptedClient([]))
        a._total_calls=2;a._usage_history=[{'prompt_tokens':4}]
        missing=a.stats()['usage_missing_requests']
        self.assertEqual(missing['prompt_tokens'],1)
        self.assertEqual(missing['completion_tokens'],2)

    def test_retry_checkpoint_failure_stops_before_sleep_or_next_request(self):
        sent=[]
        def response(request):
            sent.append(request)
            return httpx.Response(429,json={'error':{'code':'rate_limit_exceeded'}})
        with httpx.Client(transport=httpx.MockTransport(response)) as http:
            client=Client(Config(stream=False),http_client=http,sleeper=lambda _:self.fail('wait'),randomizer=lambda:0)
            a=Agent(client.config,client=client)
            def save():
                if a.last_run.get('retry'):raise OSError('retry checkpoint failed')
            a.checkpoint=save
            with self.assertRaisesRegex(OSError,'retry checkpoint failed'):a.run('task')
        self.assertEqual(len(sent),1)
        self.assertEqual(a.last_run['status'],'interrupted')

    def test_retries_share_logical_call_and_do_not_repeat_completed_tool(self):
        sent=[];sleeps=[]
        call=fixtures.call('read','read_file',{'path':'x'})
        def response(request):
            sent.append(request.content)
            if len(sent)==1:return httpx.Response(200,json={'choices':[{'message':{'tool_calls':[call]}}]})
            if len(sent)<4:return httpx.Response(429,json={'error':{'code':'rate_limit_exceeded','type':'new_api_error'}})
            return httpx.Response(200,json={'choices':[{'message':{'content':'done'}}]})
        with httpx.Client(transport=httpx.MockTransport(response)) as http:
            cfg=Config(stream=False,approval_policy='never')
            a=Agent(cfg,client=Client(cfg,http_client=http,sleeper=sleeps.append,randomizer=lambda:0))
            with patch('termux_agent.agent.execute',return_value={'content':'file'}) as execute:
                self.assertEqual(a.run('task'),'done')
            self.assertEqual(execute.call_count,1)
            self.assertEqual(a.task_state['logical_calls'],2)
            self.assertEqual(a._http_attempts_total,4)
        self.assertEqual(sent[1:],[sent[1]]*3)
