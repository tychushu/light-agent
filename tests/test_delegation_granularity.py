"""Delegation is serial and a worker's cap survives parent auto-extension."""
import copy
import os
import tempfile
import unittest
from unittest.mock import patch
import httpx

from termux_agent.agent import Agent
from termux_agent.config import Config
from termux_agent.tools import DELEGATE_TOOL
from termux_agent.client import Client
from test_subagents import ScriptClient, call


class DelegationGranularityTests(unittest.TestCase):
    def setUp(self):
        td=tempfile.TemporaryDirectory();self.addCleanup(td.cleanup)
        home=patch.dict(os.environ,{'HOME':td.name});home.start();self.addCleanup(home.stop)

    def test_effective_cap_is_advertised_without_mutating_shared_schema(self):
        before=copy.deepcopy(DELEGATE_TOOL)
        limits=[]
        for cfg in (Config(),Config(subagent_max_steps=6,max_steps=4),Config(max_task_calls=2)):
            a=Agent(cfg,client=ScriptClient(),base_system='rules')
            tool=next(t for t in a.tools if t['function']['name']=='delegate')
            limits.append(tool['function']['parameters']['properties']['max_steps']['maximum'])
        self.assertEqual(limits,[3,4,2])
        self.assertEqual(DELEGATE_TOOL,before)

    def test_parent_auto_extend_cannot_bypass_default_worker_total_cap(self):
        child=ScriptClient(*(call('read_file',{'path':str(i)},str(i)) for i in range(8)))
        parent=Agent(Config(max_steps=16,max_task_calls=64,auto_extend_segments=True,approval_policy='never'),
                     client=ScriptClient(),base_system='rules')
        with patch('termux_agent.agent.Client',return_value=child) as factory, \
             patch('termux_agent.agent.execute',return_value={'content':'ok'}) as execute:
            result=parent.delegate('inspect a small part')
        cfg=factory.call_args.args[0]
        self.assertEqual((cfg.max_steps,cfg.max_task_calls),(3,3))
        self.assertFalse(cfg.auto_continue)
        self.assertFalse(cfg.auto_extend_segments)
        self.assertEqual(result['status'],'limit')
        self.assertEqual(result['api_calls'],3)
        self.assertEqual(len(child.requests),3)
        self.assertEqual(execute.call_count,2)
        self.assertTrue(parent.config.auto_extend_segments)
        self.assertEqual(parent.config.max_task_calls,64)

    def test_smaller_requested_budget_remains_a_hard_cap(self):
        child=ScriptClient(call('read_file',{'path':'x'}))
        a=Agent(Config(approval_policy='never',auto_extend_segments=True),client=ScriptClient(),base_system='rules')
        with patch('termux_agent.agent.Client',return_value=child) as factory, \
             patch('termux_agent.agent.execute') as execute:
            result=a._delegate({'task':'small task','max_steps':1})
        self.assertEqual(result['api_calls'],1)
        self.assertEqual(factory.call_args.args[0].max_task_calls,1)
        execute.assert_not_called()

    def test_multiple_delegates_in_one_response_still_run_serially(self):
        events=[];active=[False];index=[0]
        class Child(ScriptClient):
            def __init__(self,number):super().__init__({'content':'finding'});self.number=number
            def chat(self,*args):events.append(('chat',self.number));return super().chat(*args)
            def close(self):events.append(('close',self.number));active[0]=False;super().close()
        def factory(cfg):
            self.assertFalse(active[0]);active[0]=True
            number=index[0];index[0]+=1;events.append(('open',number));return Child(number)
        calls=[call('delegate',{'task':f'part-{i}'},f'd{i}')['tool_calls'][0] for i in range(3)]
        root=ScriptClient({'tool_calls':calls},{'content':'done'})
        a=Agent(Config(approval_policy='never'),client=root,base_system='rules')
        with patch('termux_agent.agent.Client',side_effect=factory):self.assertEqual(a.run('inspect'),'done')
        self.assertEqual(events,[(action,i) for i in range(3) for action in ('open','chat','close')])

    def test_default_main_task_prompt_guides_one_small_delegation_at_a_time(self):
        client=ScriptClient({'content':'done [TASK_DONE]'})
        a=Agent(Config(auto_continue=True),client=client,base_system='rules')
        self.assertEqual(a.run('inspect'),'done')
        self.assertIn('Delegate only small subtasks, one at a time',client.requests[0][0][0]['content'])

    def test_failed_worker_cooldown_is_shared_with_next_worker_and_parent(self):
        now=[100.];wall=[1000.];sent=[];sleeps=[]
        calls=[call('delegate',{'task':f'part-{i}'},f'd{i}')['tool_calls'][0] for i in range(2)]
        def respond(request):
            sent.append((wall[0],request.content))
            if len(sent)==1:return httpx.Response(200,json={'choices':[{'message':{'tool_calls':calls}}]})
            if len(sent)==2:return httpx.Response(429,json={'error':{'code':'rate_limit_exceeded'}},headers={'Retry-After':'30'})
            return httpx.Response(200,json={'choices':[{'message':{'content':'done'}}]})
        def sleep(delay):sleeps.append(delay);now[0]+=delay;wall[0]+=delay
        with patch('termux_agent.agent.time.monotonic',side_effect=lambda:now[0]), \
             patch('termux_agent.agent.time.time',side_effect=lambda:wall[0]), \
             httpx.Client(transport=httpx.MockTransport(respond)) as http:
            cfg=Config(stream=False,retry_max_attempts=1,retry_wait_budget=90,approval_policy='never')
            def make_client(config):
                return Client(config,http_client=http,sleeper=sleep,clock=lambda:now[0],
                              wall_clock=lambda:wall[0],randomizer=lambda:0)
            a=Agent(cfg,client=make_client(cfg),base_system='rules')
            with patch('termux_agent.agent.Client',side_effect=make_client):
                self.assertEqual(a.run('inspect'),'done')
        self.assertEqual([at for at,_ in sent],[1000,1000,1030,1030])
        self.assertEqual(sleeps,[30])
        self.assertNotIn('retry_not_before',a.task_state)
