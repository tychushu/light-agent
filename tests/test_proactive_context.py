"""Cross-turn compaction, durable archives and network-free status rendering."""
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_mimo_acceptance as fixtures
from termux_agent.agent import Agent
from termux_agent.config import Config
from termux_agent.context import compact_context_messages, context_chars
from termux_agent.sessions import snapshot, validate_messages
from termux_agent.terminal_ui import Columns, format_context_status, _cell_width


def history(turns=6, body=12000):
    messages = [{'role': 'system', 'content': 'Keep every user constraint.'}]
    for index in range(turns):
        identifier = f'call{index}'
        messages.extend([
            {'role': 'user', 'content': f'Inspect step {index}; do not change unrelated files.'},
            {'role': 'assistant', 'content': '', 'reasoning_content': 'reasoning ' * 10,
             '_responses_output': [{'type': 'reasoning', 'encrypted_content': f'opaque{index}'}],
             'tool_calls': [fixtures.call(identifier, 'read_file', {'path': f'file-{index}.txt'})]},
            fixtures.tool_result(identifier, {'content': 'data ' * (body // 5), 'execution_status': 'executed'}),
            {'role': 'assistant', 'content': f'Finding {index}; artifact=file-{index}.txt'},
        ])
    return messages


class ProactiveContextTests(unittest.TestCase):
    def agent(self, messages=None, **options):
        cfg = Config(model='test-model', max_context_chars=90000, **options)
        agent = Agent(cfg, client=fixtures.ScriptedClient([{'content': 'done'}, {'content': 'next'}]),
                      base_system='Keep every user constraint.')
        if messages:
            agent.messages = copy.deepcopy(messages)
        return agent

    def test_cross_turn_history_compacts_before_limit_with_no_tools_in_latest_turn(self):
        source = history()
        agent = self.agent(source)
        self.assertLess(context_chars(source, agent.tools), agent.config.max_context_chars)
        with patch('termux_agent.agent.execute', side_effect=AssertionError('tool replay')):
            self.assertEqual(agent.run('DELIVERY.md is available; continue reviewing.'), 'done')
        self.assertEqual(len(agent.client.sent), 1)
        self.assertEqual(agent._task_compactions, 1)
        self.assertLess(context_chars(agent.client.sent[0], agent.tools), 90000 * .75)
        humans = [m for m in agent.client.sent[0] if m['role'] == 'user'
                  and not m['content'].startswith('[Context archive:')]
        self.assertEqual(humans[:-1], [m for m in source if m['role'] == 'user'])
        self.assertEqual(agent.in_memory_archives[0]['messages'][:-1], source)
        validate_messages(agent.messages)
        before = agent._task_compactions
        self.assertEqual(agent.run('short follow-up'), 'next')
        self.assertEqual(agent._task_compactions, before)  # headroom prevents immediate re-compaction

    def test_pure_chat_can_reduce_old_answers_and_retains_all_human_constraints(self):
        agent = self.agent()
        for index in range(6):
            agent.messages.extend([{'role': 'user', 'content': f'constraint-{index}'},
                                   {'role': 'assistant', 'content': f'answer-{index} ' + 'x' * 18000}])
        originals = copy.deepcopy(agent.messages)
        agent.run('next')
        sent = agent.client.sent[0]
        for message in originals:
            if message['role'] == 'user':
                self.assertIn(message, sent)
        self.assertIn(originals[-1], sent)
        self.assertNotIn(originals[2], sent)
        self.assertIn('assistant_excerpt', sent[1]['content'])
        self.assertEqual(agent.in_memory_archives[0]['messages'][:-1], originals)

    def test_unknown_write_group_and_recent_protocol_items_remain_verbatim(self):
        source = history(6)
        call = fixtures.call('unknown-write', 'write_file', {'path': 'unknown.txt', 'content': 'secret body'})
        unknown = [{'role': 'assistant', 'content': '', 'tool_calls': [call],
                    '_responses_output': [{'type': 'reasoning', 'encrypted_content': 'signature'}]},
                   fixtures.tool_result('unknown-write', {'error': 'Interrupted checkpoint: tool outcome is unknown.',
                                                        'execution_status': 'unknown', 'write_status': 'unknown'})]
        source[2:2] = copy.deepcopy(unknown)
        original = copy.deepcopy(source)
        result, details = compact_context_messages(source, 2, 'archive-id')
        self.assertGreater(details['omitted_groups'], 0)
        self.assertEqual(source, original)
        for message in [*unknown, *source[-3:]]:
            self.assertIn(message, result)
        validate_messages(result)
        agent = self.agent(result)
        self.assertIn(agent._call_fingerprint(call), agent._scan_unsafe_writes(result))

    def test_repeated_compaction_has_bounded_memo_and_links_to_complete_previous_archive(self):
        source = history(80, 2000)
        compacted, _ = compact_context_messages(source, 2, 'archive-one')
        self.assertLess(len(compacted[1]['content']), 10000)
        compacted.extend(history(4, 4000)[1:])
        result, details = compact_context_messages(compacted, 1, 'archive-two')
        self.assertGreater(details['omitted_groups'], 0)
        notes = [m for m in result if m.get('content', '').startswith('[Context archive:')]
        self.assertEqual(len(notes), 1)
        self.assertIn('archive-two', notes[0]['content'])
        self.assertLess(len(notes[0]['content']), 10000)
        self.assertTrue(compacted[1]['content'].startswith('[Context archive: archive-one] '))

    def test_disabled_auto_compaction_and_unshrinkable_input_block_before_http(self):
        source = history()
        agent = self.agent(source, auto_compact=False)
        agent.config.max_context_chars = 60000
        self.assertIn('/compact', agent.run('next'))
        self.assertEqual(agent.client.sent, [])
        self.assertEqual(agent.messages[:-1], source)
        agent = self.agent()
        self.assertIn('用户指令', agent.run('x' * 100000))
        self.assertEqual(agent.client.sent, [])
        self.assertEqual(agent.messages[-1]['content'], 'x' * 100000)
        self.assertEqual(agent.in_memory_archives, [])

    def test_token_headroom_triggers_compaction_even_below_character_threshold(self):
        agent = self.agent(history(7, 6000), context_window_tokens=18000, output_reserve_tokens=4000)
        agent.config.max_context_chars = 500000
        self.assertLess(context_chars(agent.messages, agent.tools), .2 * agent.config.max_context_chars)
        agent.run('next')
        self.assertTrue(agent.in_memory_archives)
        self.assertEqual(len(agent.client.sent), 1)
        token = agent.token_counter.measure(agent.client.sent[0], agent.tools, allow_network=False)
        self.assertLess(token['prompt_tokens'] + token['output_reserve_tokens'], 18000)

    def test_archive_failure_stops_before_network_and_keeps_source(self):
        agent = self.agent(history())
        def fail(*args):
            raise OSError('archive write failed')
        agent.branch_after_compact = fail
        before = copy.deepcopy(agent.messages)
        with self.assertRaises(OSError):
            agent.run('next')
        self.assertEqual(agent.client.sent, [])
        self.assertEqual(agent.messages[:-1], before)

    def test_status_no_network_uses_cached_window_includes_schemas_and_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.agent(auto_continue=True, token_count_mode='local')
            agent.token_counter.cache_path = Path(tmp)/'cache.json'
            agent.token_counter._cache_write({'context_window_tokens': 32768, 'metadata_checked': True})
            with patch.object(agent.token_counter, '_request', side_effect=AssertionError('status network')):
                status = agent.context_status()
            self.assertEqual(status['tokens']['context_window_tokens'], 32768)
            self.assertEqual(status['tokens']['source'], 'estimate')
            self.assertGreater(status['chars'], context_chars(agent.messages, agent.tools))
            rendered = format_context_status(status)
            self.assertIn('Chars=', rendered)
            self.assertIn('Tokens≈', rendered)
            self.assertIn('/32,768', rendered)
            self.assertIn('Auto=75%', rendered)
            self.assertIn(f"({status['chars']/status['limit']:.0%})", rendered)

    def test_unknown_window_is_explicit_and_off_does_not_invent_token_percentage(self):
        agent = self.agent()
        text = format_context_status(agent.context_status())
        self.assertIn('未知窗口', text)
        agent.config.token_count_mode = 'off'
        self.assertIn('Tokens=未计数', format_context_status(agent.context_status()))

    def test_status_wraps_on_mobile_terminal_without_clipping_numbers(self):
        status = self.agent(context_window_tokens=32768).context_status()
        for width in (32, 51, 80, 120):
            out = io.StringIO()
            Columns(out, width=lambda:width).message('Status',format_context_status(status))
            self.assertTrue(all(_cell_width(line)<=width for line in out.getvalue().splitlines()))
            # Wrapping may break labels, but must not silently discard context data.
            self.assertIn('Context=',out.getvalue())

    def test_config_profile_overrides_and_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'config.toml'
            path.write_text('auto_compact=false\nauto_compact_keep_turns=4\n[apis.cloud]\n'
                            'base_url="https://example.test/v1"\nmodel="m"\nauto_compact=true\n'
                            'auto_compact_keep_turns=1\n')
            self.assertFalse(Config.load(path, 'default').auto_compact)
            cloud = Config.load(path, 'cloud')
            self.assertTrue(cloud.auto_compact)
            self.assertEqual(cloud.auto_compact_keep_turns, 1)
        for changes in ({'auto_compact':'true'}, {'auto_compact_keep_turns':True},
                        {'auto_compact_keep_turns':0}):
            with self.assertRaises(ValueError):
                Config(**changes).validate()


class DurableContextTests(fixtures.AcceptanceTestCase):
    def test_loaded_large_history_branches_on_continue_original_row_unchanged(self):
        config_path = self.root/'config.toml'
        config_path.write_text('base_url="http://127.0.0.1:9/v1"\nmodel="acceptance-model"\n'
                               'stream=false\nmax_context_chars=40000\nauto_continue=false\n')
        cfg = Config.load(config_path)
        agent = Agent(cfg, client=fixtures.ScriptedClient([]), base_system='Keep every user constraint.')
        agent.messages = history()
        agent.messages.append({'role':'user','content':'DELIVERY.md is available'})
        agent._ensure_task_state('DELIVERY.md is available')
        scope = {'directory':str(self.root),'config_path':str(config_path),'instructions':'',
                 'session_file':None,'disabled':True,'instruction_path':None}
        original_id = 'abcdef123456'
        with fixtures.open_store(self.root/'store') as store:
            store.save(original_id,0,snapshot(agent,scope),title='original')
            before = store.db.execute('select * from sessions where id=?',(original_id,)).fetchone()
        output, requests = self.run_cli(['--session',original_id],['/continue 1','/exit'],
                                       [{'content':'continued'}],store=self.root/'store')
        self.assertIn('已压缩上下文', output)
        request = json.loads(requests.read_text().splitlines()[0])
        self.assertLess(context_chars(request['messages'],request['tools']),40000)
        with fixtures.open_store(self.root/'store') as store:
            self.assertEqual(before,store.db.execute('select * from sessions where id=?',(original_id,)).fetchone())
            self.assertEqual(len(store.ids()),2)
            branch_id = next(i for i in store.ids() if i!=original_id)
            payload=store.load(branch_id)[1]
            self.assertEqual(payload['branch']['archive_session_id'],original_id)
            self.assertEqual(payload['task_state']['compactions'],1)
        self.assertEqual(len(requests.read_text().splitlines()),1)

    def test_manual_compact_without_tools_in_last_turn_reduces_history_without_http(self):
        config_path = self.root/'config.toml'
        config_path.write_text('model="acceptance-model"\n')
        cfg = Config.load(config_path)
        agent = Agent(cfg,client=fixtures.ScriptedClient([]))
        agent.messages = history()
        agent.messages.append({'role':'user','content':'latest user instruction'})
        scope = {'directory':str(self.root),'config_path':str(config_path),'instructions':'',
                 'session_file':None,'disabled':True,'instruction_path':None}
        with fixtures.open_store(self.root/'store') as store:
            store.save('abcdef123456',0,snapshot(agent,scope))
        output,requests = self.run_cli(['--session','abcdef123456'],['/compact','/exit'],
                                      store=self.root/'store',bomb_network=True)
        self.assertIn('已压缩上下文',output)
        self.assertEqual(requests.read_text(),'')
