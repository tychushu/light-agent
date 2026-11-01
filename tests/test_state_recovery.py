"""Unexpected exits, failed persistence and hard-kill restoration invariants."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import test_mimo_acceptance as fixtures
from termux_agent.agent import Agent
from termux_agent.config import Config
from termux_agent.sessions import SessionStore, restore, snapshot, validate_messages


class StateRecoveryTests(unittest.TestCase):
    def make(self, **changes):
        cfg=Config(**changes)
        return Agent(cfg,client=fixtures.ScriptedClient([{'content':'done'}]),base_system='rules')

    def test_unexpected_preflight_error_finalizes_and_preserves_original_exception(self):
        a=self.make();saved=[];a.checkpoint=lambda:saved.append(snapshot(a,{},checkpoint=True))
        error=RuntimeError('preflight failed')
        with patch.object(a.token_counter,'measure',side_effect=error),self.assertRaises(RuntimeError) as caught:
            a.run('preserve this goal')
        self.assertIs(caught.exception,error)
        self.assertEqual(a.last_run['status'],a.task_state['status'])
        self.assertEqual(a.last_run['status'],'interrupted')
        self.assertTrue(a.last_run['can_continue'])
        self.assertEqual(a._segment_started,0)
        self.assertEqual(saved[-1]['last_run']['status'],'interrupted')
        self.assertEqual(a.task_state['goal'],'preserve this goal')
        task_id=a.task_state['id']
        self.assertEqual(a.continue_task(),'done')
        self.assertEqual(a.task_state['id'],task_id)
        self.assertEqual(a.task_state['logical_calls'],1)
        self.assertEqual(a._conversation_count,1)

    def test_control_setup_base_exceptions_are_checkpointed_and_propagated(self):
        for error in (KeyboardInterrupt(),SystemExit(19)):
            a=self.make();saved=[];a.checkpoint=lambda:saved.append(snapshot(a,{},checkpoint=True))
            with patch.object(a.client,'set_control_markers',side_effect=error),self.assertRaises(type(error)) as caught:
                a.run('task')
            self.assertIs(caught.exception,error)
            self.assertEqual(a.last_run['status'],'interrupted')
            self.assertEqual(saved[-1]['task_state']['status'],'interrupted')
            self.assertEqual(a.client.sent,[])

    def test_handled_network_error_and_keyboard_interrupt_keep_their_normal_stop_paths(self):
        for error,status in ((RuntimeError('HTTP failed'),'error'),(KeyboardInterrupt(),'interrupted')):
            a=self.make();a.client=fixtures.ScriptedClient([{'_raise':error}])
            a.run('task')
            self.assertEqual(a.last_run['status'],status)
            self.assertNotIn('unexpectedly',a.last_run['message'])
            self.assertEqual(a.task_state['logical_calls'],1)

    def test_failed_cleanup_never_masks_the_original_error(self):
        a=self.make();calls=[]
        def save():
            calls.append(1)
            if len(calls)>1:raise OSError('cleanup disk failure')
        a.checkpoint=save;primary=RuntimeError('primary preflight error')
        with patch.object(a.token_counter,'measure',side_effect=primary),self.assertRaises(RuntimeError) as caught:
            a.run('task')
        self.assertIs(caught.exception,primary)
        self.assertIn('OSError',' '.join(primary.__notes__))
        self.assertEqual(a.last_run['checkpoint_error'],'OSError')
        self.assertEqual(a.last_run['status'],'interrupted')

    def test_request_reservation_checkpoint_failure_propagates_without_http(self):
        a=self.make();primary=OSError('request checkpoint failed');calls=[]
        def save():
            calls.append(1)
            if len(calls)==2:raise primary
        a.checkpoint=save
        with self.assertRaises(OSError) as caught:a.run('task')
        self.assertIs(caught.exception,primary)
        self.assertEqual(a.client.sent,[])
        self.assertEqual(a.last_run['status'],'interrupted')
        self.assertEqual(a.task_state['logical_calls'],1) # reserved conservatively, not an HTTP attempt

    def test_invalid_task_prompt_still_preserves_new_user_input(self):
        a=self.make(auto_continue=True)
        with patch('termux_agent.agent.load_prompt_file',side_effect=ValueError('bad task prompt')), \
                self.assertRaises(ValueError):
            a.run('do not lose these constraints')
        self.assertEqual(a.messages[-1]['content'],'do not lose these constraints')
        self.assertEqual(a.last_run['status'],'interrupted')

    def test_pending_batch_becomes_unknown_without_claiming_execution_or_replaying_writes(self):
        calls=[fixtures.call(str(i),'write_file',{'path':f'never-write-{i}','content':'body'}) for i in range(2)]
        a=self.make(approval_policy='never');a.client=fixtures.ScriptedClient([
            {'tool_calls':calls},{'tool_calls':calls},{'content':'done'}])
        failed=[]
        def save():
            if a.messages[-1].get('tool_calls') and not failed:
                failed.append(True);raise OSError('failed before tools')
        a.checkpoint=save
        with patch('termux_agent.agent.execute',side_effect=AssertionError('unknown write replayed')):
            with self.assertRaises(OSError):a.run('write once')
            validate_messages(a.messages)
            outcomes=[json.loads(m['content']) for m in a.messages if m['role']=='tool']
            self.assertEqual([m['execution_status'] for m in outcomes],['unknown','unknown'])
            self.assertEqual(a._tool_calls_executed,0)
            self.assertEqual(a.continue_task(),'done')
        self.assertEqual(a._tool_calls_executed,0)

    def test_repeated_continue_does_not_append_controls_after_unanswered_human_input(self):
        a=self.make(max_context_chars=1000)
        a.run('original constraints')
        task_id=a.task_state['id'];before=copy.deepcopy(a.messages)
        for _ in range(3):a.continue_task()
        self.assertEqual(a.messages,before)
        self.assertEqual(a.task_state['id'],task_id)
        self.assertEqual(a.task_state['goal'],'original constraints')
        self.assertEqual(a._conversation_count,1)
        self.assertEqual(a.client.sent,[])

    def test_empty_tails_and_identical_resume_controls_normalize_without_merging_human_text(self):
        for content in (None,'','   ',[]):
            a=self.make();a.messages.append({'role':'user','content':content})
            result=a._resume_messages('[TASK_RESUME]\nresume')
            self.assertEqual(result[-1]['content'],'[TASK_RESUME]\nresume')
        a=self.make();human={'role':'user','content':'additional constraint {exact}'}
        a.messages.extend([{'role':'user','content':'original'},human])
        self.assertEqual(a._resume_messages('[TASK_RESUME]\nresume'),a.messages)
        a.messages.extend([{'role':'user','content':'[TASK_RESUME]\nresume'}]*2)
        normalized=a._resume_messages('[TASK_RESUME]\nresume')
        self.assertEqual(sum(m.get('content')=='[TASK_RESUME]\nresume' for m in normalized),1)
        self.assertIn(human,normalized)


class DurableStateTests(fixtures.AcceptanceTestCase):
    def seed(self):
        config_path=self.root/'config.toml'
        config_path.write_text('model="recovery-model"\nmax_task_calls=7\n')
        a=Agent(Config.load(config_path),client=fixtures.ScriptedClient([]),base_system='rules')
        a.messages.append({'role':'user','content':'original goal'})
        a._ensure_task_state('original goal');a.task_state['logical_calls']=3
        a.last_run={'status':'running','stop_reason':'running'}
        scope={'directory':str(self.root),'config_path':str(config_path),'instructions':'',
               'session_file':None,'disabled':True,'instruction_path':None}
        return a,scope

    def build(self,cfg,text,base_system=None,**kwargs):
        return Agent(cfg,client=kwargs.get('client') or fixtures.ScriptedClient([]),base_system=base_system)

    def test_restore_repairs_only_in_memory_preserving_payload_budget_and_database(self):
        a,scope=self.seed();payload=snapshot(a,scope);original=copy.deepcopy(payload)
        with fixtures.open_store(self.root/'store') as store:
            store.save('abcdef123456',0,payload)
            before=store.db.execute('select * from sessions').fetchall()
            _,restored,_=restore(payload,self.build)
            self.assertEqual(restored.last_run['status'],'interrupted')
            self.assertEqual(restored.task_state['status'],'interrupted')
            self.assertTrue(restored.last_run['can_continue'])
            self.assertEqual(restored.task_state['logical_calls'],3)
            self.assertEqual(restored.task_state['max_logical_calls'],7)
            self.assertEqual(payload,original)
            self.assertEqual(store.db.execute('select * from sessions').fetchall(),before)

    def test_recovered_running_snapshot_does_not_override_prior_approval_denial(self):
        a,scope=self.seed();a.messages.extend([
            {'role':'assistant','tool_calls':[fixtures.call('denied','write_file',{'path':'x','content':'x'})]},
            fixtures.tool_result('denied',{'error':'approval denied','execution_status':'denied'})])
        _,restored,_=restore(snapshot(a,scope),self.build)
        self.assertEqual(restored.last_run['status'],'interrupted')
        self.assertFalse(restored.last_run['can_continue'])
        with self.assertRaises(ValueError):restored.continue_task()

    def test_normal_terminal_states_are_not_reclassified(self):
        for status in ('complete','blocked','error','limit','interrupted'):
            a,scope=self.seed();a.last_run.update(status=status,stop_reason=status,can_continue=False)
            a.task_state.update(status=status,stop_reason=status)
            payload=snapshot(a,scope);_,restored,_=restore(payload,self.build)
            self.assertEqual(restored.last_run['status'],status)
            self.assertFalse(restored.last_run['can_continue'])

    def test_file_text_mentioning_denial_is_not_controller_approval_state(self):
        a,scope=self.seed();a.messages.extend([
            {'role':'assistant','tool_calls':[fixtures.call('read','read_file',{'path':'guide.md'})]},
            fixtures.tool_result('read',{'content':'Example text: approval denied', 'execution_status':'executed'})])
        _,restored,_=restore(snapshot(a,scope),self.build)
        self.assertTrue(restored.last_run['can_continue'])

    def test_hard_exit_after_side_effect_recovers_running_and_never_replays_unknown_write(self):
        a,scope=self.seed();target=self.root/'side-effect.txt';store_root=self.root/'hard-store'
        script=r'''
import json,os,sys
from pathlib import Path
from unittest.mock import patch
from termux_agent.agent import Agent
from termux_agent.config import Config
from termux_agent.sessions import SessionStore,snapshot
config_path,store_path,target=sys.argv[1:]
call={'id':'unknown','type':'function','function':{'name':'write_file','arguments':json.dumps({'path':target,'content':'once'})}}
class Reply:
    last_usage=None
    def chat(self,*a):return {'tool_calls':[call]}
    def close(self):pass
a=Agent(Config.load(config_path),client=Reply(),base_system='rules');a.config.approval_policy='never'
store=SessionStore(store_path);revision=0
scope={'directory':str(Path(target).parent),'config_path':config_path,'instructions':'','session_file':None,'disabled':True,'instruction_path':None}
def save():
    global revision
    revision=store.save('abcdef123456',revision,snapshot(a,scope,checkpoint=True))
def execute(*args,**kwargs):
    Path(target).write_text('once');os._exit(17)
a.checkpoint=save
with patch('termux_agent.agent.execute',execute):a.run('write once')
'''
        result=subprocess.run([sys.executable,'-c',script,scope['config_path'],str(store_root),str(target)],
                              cwd=fixtures.REPO_ROOT,capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,17,result.stderr)
        self.assertEqual(target.read_text(),'once')
        with fixtures.open_store(store_root) as store:
            before=store.db.execute('select * from sessions').fetchall()
            payload=store.load('abcdef123456')[1]
            self.assertEqual(payload['last_run']['status'],'running')
            self.assertEqual(payload['task_state']['logical_calls'],1)
            _,restored,_=restore(payload,self.build)
            self.assertEqual(restored.last_run['status'],'interrupted')
            call=fixtures.call('retry','write_file',{'path':str(target),'content':'once'})
            restored.config.approval_policy='never'
            restored.client=fixtures.ScriptedClient([{'tool_calls':[call]},{'content':'done'}])
            with patch('termux_agent.agent.execute',side_effect=AssertionError('write replayed')):
                self.assertEqual(restored.continue_task(),'done')
            self.assertEqual(target.read_text(),'once')
            self.assertEqual(store.db.execute('select * from sessions').fetchall(),before)
