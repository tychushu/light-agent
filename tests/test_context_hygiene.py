"""Proactive old-tool cleanup preserves active reasoning and completed evidence."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_mimo_acceptance as fixtures
from termux_agent.agent import Agent
from termux_agent.config import Config
from termux_agent.context import compact_tool_history_messages, context_chars
from termux_agent.sessions import validate_messages


def group(index,size=6000,name='read_file',result=None):
    args={'path':f'file-{index}.txt'}
    if name=='write_file':args['content']='body'
    return [{'role':'assistant','content':'inspection', 'reasoning_content':f'think-{index}',
             '_responses_output':[{'type':'reasoning','encrypted_content':f'opaque-{index}'}],
             'tool_calls':[fixtures.call(f'c{index}',name,args)]},
            fixtures.tool_result(f'c{index}',result or {'content':'data '* (size//5),
                                                       'execution_status':'executed'})]


def history():
    messages=[{'role':'system','content':'rules'}]
    for i in range(6):
        messages.extend([{'role':'user','content':f'constraint-{i}'},*group(i),
                         {'role':'assistant','content':f'conclusion-{i}'}])
    return messages


class ContextHygieneTests(unittest.TestCase):
    def make(self,**changes):
        return Agent(Config(**changes),client=fixtures.ScriptedClient([{'content':'done'}]),base_system='rules')

    def test_earlier_groups_reduce_while_current_chain_humans_and_final_answers_stay_exact(self):
        messages=history()+[{'role':'user','content':'active human goal'}]
        current=[*group(10),*group(11),*group(12)]
        messages.extend(current);before=copy.deepcopy(messages)
        reduced,detail=compact_tool_history_messages(messages,2,'archive')
        self.assertEqual(detail['omitted_groups'],4)
        self.assertEqual(sum(m['role']=='tool' for m in reduced),5)
        for message in [*current,*messages[18:20]]: # recent old group and full active chain
            if message['role']!='user':self.assertIn(message,reduced)
        for message in messages:
            if message['role']=='user' or (message['role']=='assistant' and not message.get('tool_calls')):
                self.assertIn(message,reduced)
        validate_messages(reduced)
        self.assertEqual(messages,before)
        self.assertLess(context_chars(reduced),context_chars(messages))

    def test_default_cleanup_is_below_threshold_and_only_at_new_human_boundary(self):
        a=self.make();a.messages=history()
        before=copy.deepcopy(a.messages)
        self.assertLess(context_chars(before,a.tools),a.config.max_context_chars*.75)
        self.assertEqual(a.run('new task'),'done')
        self.assertEqual(sum(m['role']=='tool' for m in a.client.sent[0]),2)
        self.assertEqual(len(a.in_memory_archives),1)
        self.assertEqual(a.in_memory_archives[0]['messages'][:-1],before)
        self.assertEqual(a.task_state['compactions'],1)
        a.client=fixtures.ScriptedClient([{'content':'continued'}])
        self.assertEqual(a.continue_task(),'continued')
        self.assertEqual(len(a.in_memory_archives),1)

    def test_long_active_task_is_not_trimmed_on_continue_or_each_tool_step(self):
        a=self.make();a.messages=[{'role':'system','content':'rules'},{'role':'user','content':'original task'}]
        for i in range(8):a.messages.extend(group(i))
        a._ensure_task_state('original task')
        self.assertEqual(a.continue_task(),'done')
        self.assertEqual(sum(m['role']=='tool' for m in a.client.sent[0]),8)
        self.assertEqual(a.in_memory_archives,[])
        b=self.make(approval_policy='never')
        b.client=fixtures.ScriptedClient([{'tool_calls':[fixtures.call(str(i),'read_file',{'path':'x'})]}
                                         for i in range(4)]+[{'content':'done'}])
        with patch('termux_agent.agent.execute',return_value={'content':'body '*1000}):b.run('one task')
        self.assertEqual(sum(m['role']=='tool' for m in b.client.sent[-1]),4)
        self.assertEqual(b.in_memory_archives,[])

    def test_unknown_and_failed_write_results_are_never_folded_away(self):
        for result in ({'error':'unknown','execution_status':'unknown','write_status':'unknown'},
                       {'error':'write failed','write_status':'failed'}):
            messages=[{'role':'system','content':'rules'},{'role':'user','content':'write'}]
            protected=group(99,name='write_file',result=result)
            messages.extend(protected)
            messages.extend(history()[1:]);messages.append({'role':'user','content':'next task'})
            reduced,detail=compact_tool_history_messages(messages,2,'archive')
            self.assertGreater(detail['omitted_groups'],0)
            for message in protected:self.assertIn(message,reduced)

    def test_disable_and_tiny_results_leave_history_unchanged(self):
        messages=history()+[{'role':'user','content':'next'}]
        self.assertEqual(compact_tool_history_messages(messages,0,'archive')[0],messages)
        a=self.make(auto_compact=False,auto_compact_task=False);a.messages=history()
        a.run('next')
        self.assertEqual(sum(m['role']=='tool' for m in a.client.sent[0]),6)
        self.assertEqual(a.in_memory_archives,[])
        tiny=[{'role':'system','content':'rules'},{'role':'user','content':'old'}]
        for i in range(3):tiny.extend(group(i,size=0))
        tiny.append({'role':'user','content':'next'})
        self.assertEqual(compact_tool_history_messages(tiny,2,'archive')[0],tiny)

    def test_repeated_cleanup_replaces_archive_memos_and_keeps_a_bound(self):
        messages=history()+[{'role':'user','content':'new task'}]
        first,_=compact_tool_history_messages(messages,2,'archive-one')
        first.extend([*group(20),*group(21),*group(22),{'role':'user','content':'next task'}])
        second,detail=compact_tool_history_messages(first,2,'archive-two')
        self.assertGreater(detail['omitted_groups'],0)
        memos=[m for m in second if str(m.get('content','')).startswith('[Context archive:')]
        self.assertEqual(len(memos),1)
        self.assertIn('archive-two',memos[0]['content'])
        self.assertLess(len(memos[0]['content']),10000)

    def test_archive_failure_stops_before_send_and_preserves_full_input(self):
        a=self.make();a.messages=history();before=copy.deepcopy(a.messages)
        def fail(*args):raise OSError('archive failed')
        a.branch_after_compact=fail
        with self.assertRaises(OSError):a.run('next task')
        self.assertEqual(a.client.sent,[])
        self.assertEqual(a.messages[:-1],before)
        self.assertEqual(a.last_run['status'],'interrupted')

    def test_configuration_supports_per_profile_retention_and_rejects_invalid_values(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'config.toml'
            path.write_text('tool_history_keep_groups=5\n[apis.local]\n'
                            'base_url="http://127.0.0.1:9/v1"\nmodel="x"\ntool_history_keep_groups=0\n')
            self.assertEqual(Config.load(path,'default').tool_history_keep_groups,5)
            self.assertEqual(Config.load(path,'local').tool_history_keep_groups,0)
        for value in (True,-1,101,1.5,'2'):
            with self.assertRaises(ValueError):Config(tool_history_keep_groups=value).validate()
