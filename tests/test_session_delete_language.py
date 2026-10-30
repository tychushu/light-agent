"""Deletion chooses stable visible targets; UI language never rewrites conversations."""
import ast
import copy
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import test_session_selection as fixtures
from termux_agent.agent import Agent
from termux_agent.config import Config
from termux_agent.i18n import text, help_text
from termux_agent.sessions import SessionStore, restore
from termux_agent.terminal_ui import session_table, format_context_status, _cell_width


class SessionDeleteTests(unittest.TestCase):
    setUp = fixtures.SessionSelectionTests.setUp
    tearDown = fixtures.SessionSelectionTests.tearDown
    run_cli = fixtures.SessionSelectionTests.run_cli
    add_session = fixtures.SessionSelectionTests.add_session
    set_order = fixtures.SessionSelectionTests.set_order

    def seed(self):
        self.add_session('aaaaaaaaaaaa','Alpha',1_900_000_000)
        self.add_session('bbbbbbbbbbbb','Beta',1_800_000_000)
        self.set_order('aaaaaaaaaaaa','bbbbbbbbbbbb')

    def english(self):
        self.config.write_text('ui_language="en"\n'+self.config.read_text())

    def test_delete_ordinal_uses_cached_list_despite_reordering(self):
        self.seed()
        before=self.store.load('bbbbbbbbbbbb')
        def reorder():self.set_order('bbbbbbbbbbbb','aaaaaaaaaaaa')
        output,prompts,choices,calls=self.run_cli(
            ['/sessions',fixtures.InputHook(reorder),'/session delete 1','yes','/exit'],tty=True)
        self.assertEqual(self.store.ids(),['bbbbbbbbbbbb'])
        self.assertEqual(self.store.load('bbbbbbbbbbbb'),before)
        self.assertEqual(choices,['1','2'])  # deletion does not silently renumber the displayed list
        self.assertTrue(any('aaaaaaaaaaaa' in prompt for prompt in prompts))
        self.assertIn('Alpha',output)
        self.assertIn('已删除会话',output)
        self.assertEqual(calls,[])

    def test_no_argument_picker_selects_and_confirms_one_target(self):
        self.seed()
        output,prompts,_,_=self.run_cli(['/session delete','2','y','/exit'],tty=True)
        self.assertEqual(self.store.ids(),['aaaaaaaaaaaa'])
        self.assertIn('删除序号或ID',prompts[1])
        self.assertIn('bbbbbbbbbbbb',prompts[2])
        self.assertIn('Beta',output)

    def test_picker_blank_eof_and_ctrl_c_cancel_only_picker(self):
        self.seed()
        before=self.store.ids()
        for cancel in ('',EOFError(),KeyboardInterrupt()):
            with self.subTest(cancel=type(cancel).__name__):
                output,prompts,_,_=self.run_cli(['/session delete',cancel,'/exit'],tty=True)
                self.assertEqual(self.store.ids(),before)
                self.assertIn('已取消删除',output)
                self.assertEqual(len(prompts),3)

    def test_confirmation_no_eof_and_ctrl_c_cancel_without_leaving_repl(self):
        self.seed()
        for cancel in ('n','',EOFError(),KeyboardInterrupt()):
            output,prompts,_,_=self.run_cli(['/session delete aaaaaaaaaaaa',cancel,'/exit'],tty=True)
            self.assertEqual(len(self.store.ids()),2)
            self.assertIn('已取消删除',output)
            self.assertEqual(len(prompts),3)

    def test_active_session_and_invalid_numbers_are_not_deleted(self):
        self.seed()
        before=self.store.load('aaaaaaaaaaaa')
        output,_,_,_=self.run_cli(['/sessions','/session delete 1 --yes','/session delete 99 --yes','/exit'],
                                   args=['--session','aaaaaaaaaaaa'])
        self.assertEqual(len(self.store.ids()),2)
        self.assertEqual(self.store.load('aaaaaaaaaaaa'),before)
        self.assertIn('请先切换到其他会话',output)
        self.assertIn('超出最后一次',output)

    def test_nontty_picker_and_unconfirmed_delete_do_not_consume_next_command(self):
        self.seed()
        output,prompts,_,_=self.run_cli(['/session delete','/session delete 1','/exit'])
        self.assertEqual(len(prompts),3)
        self.assertEqual(len(self.store.ids()),2)
        self.assertIn('--yes',output)

    def test_explicit_yes_deletes_by_number_and_missing_list_never_guesses(self):
        self.seed()
        output,_,_,_=self.run_cli(['/session delete 1 --yes','/sessions find Beta',
                                 '/session delete 1 --yes','/session delete --yes','/exit'])
        self.assertEqual(self.store.ids(),['aaaaaaaaaaaa'])
        self.assertIn('先用 /sessions',output)
        self.assertIn('用法：/session delete',output)

    def test_numeric_full_id_takes_precedence_over_ordinal(self):
        self.seed()
        self.add_session('123456789012','Numeric',1_700_000_000)
        self.run_cli(['/sessions','/session delete 123456789012 --yes','/exit'])
        self.assertCountEqual(self.store.ids(),['aaaaaaaaaaaa','bbbbbbbbbbbb'])

    def test_deletion_confirmation_detects_concurrent_title_or_revision_changes(self):
        self.seed()
        def rename():self.store.rename('aaaaaaaaaaaa','Changed by another writer')
        output,_,_,_=self.run_cli(['/session delete aaaaaaaaaaaa',fixtures.InputHook(rename),'y','/exit'],tty=True)
        self.assertEqual(len(self.store.ids()),2)
        self.assertIn('重新列出会话并确认',output)
        metadata=self.store.deletion_target('aaaaaaaaaaaa')
        revision,payload=self.store.load('aaaaaaaaaaaa')
        self.store.save('aaaaaaaaaaaa',revision,payload)
        with self.assertRaises(ValueError):self.store.delete('aaaaaaaaaaaa',expected=metadata)

    def test_deletion_uses_only_metadata_without_decoding_history(self):
        self.seed()
        with patch('termux_agent.sessions.json.loads',side_effect=AssertionError('private history decoded')):
            metadata=self.store.deletion_target('aaaaaaaaaaaa')
            self.store.delete('aaaaaaaaaaaa',expected=metadata)
        self.assertEqual(self.store.ids(),['bbbbbbbbbbbb'])

    def test_english_ui_help_picker_confirmation_status_and_errors(self):
        self.english();self.seed()
        output,prompts,_,_=self.run_cli(['/help','/session delete','2','y','/resume 99','/exit'],tty=True)
        self.assertIn('Delete by displayed number or ID',output)
        self.assertIn('Session number or ID to delete',prompts[2])
        self.assertIn('Delete session bbbbbbbbbbbb?',prompts[3])
        self.assertIn('Session deleted.',output)
        self.assertIn('outside the last displayed list',output)
        self.assertIn('Context=Normal',output)
        self.assertIn('/unknown',output)
        self.assertIn('window',output)
        self.assertNotRegex(output,r'[\u3400-\u9fff]')

    def test_restored_and_unavailable_profiles_use_current_ui_preference(self):
        self.seed();self.english()
        self.config.write_text(self.config.read_text().replace('old-model','new-model'))
        before=self.store.db.execute('select * from sessions where id=?',('aaaaaaaaaaaa',)).fetchone()
        output,_,_,_=self.run_cli(['/help','/history','/exit'],args=['--session','aaaaaaaaaaaa'])
        self.assertIn('History-only mode:',output)
        self.assertIn('archived answer',output)
        self.assertNotRegex(output,r'[\u3400-\u9fff]')
        self.assertEqual(self.store.db.execute('select * from sessions where id=?',('aaaaaaaaaaaa',)).fetchone(),before)


class LanguageTests(unittest.TestCase):
    def test_language_config_is_global_validated_and_read_without_profiles(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'config.toml'
            p.write_text('ui_language="en"\n[apis.cloud]\nbase_url="https://example.test/v1"\n'
                         'model="m"\nui_language="zh"\n')
            self.assertEqual(Config.read_ui_language(p),'en')
            self.assertEqual(Config.load(p,'cloud').ui_language,'en')
            for invalid in (True,None,'auto','fr',[],{}):
                with self.subTest(value=invalid),self.assertRaises(ValueError):Config(ui_language=invalid).validate()

    def test_startup_help_uses_selected_config_before_loading_any_api(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'config.toml';p.write_text('ui_language="en"\ndefault_api="missing"\n')
            result=subprocess.run([sys.executable,'-m','termux_agent.cli','--config',str(p),'--help'],
                                  text=True,capture_output=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('Path to a TOML configuration file',result.stdout)
            self.assertNotRegex(result.stdout,r'[\u3400-\u9fff]')

    def test_model_user_text_and_prompt_snapshots_are_never_translated(self):
        class Reply:
            def chat(self,messages,tools):return {'content':'没有已保存会话。 Session deleted.'}
            def close(self):pass
        outputs=[]
        for language in ('zh','en'):
            a=Agent(Config(ui_language=language),client=Reply(),base_system='An unchanged system prompt.')
            answer=a.run('Session deleted. 我自己的文本')
            self.assertEqual(answer,'没有已保存会话。 Session deleted.')
            outputs.append(a.messages)
        self.assertEqual(outputs[0],outputs[1])

    def test_controller_errors_use_selected_language_and_provider_error_body_is_preserved(self):
        class Fail:
            def chat(self,*args):raise RuntimeError('原始上游错误: {details}')
            def close(self):pass
        a=Agent(Config(ui_language='en'),client=Fail())
        self.assertEqual(a.run('test'),'LLM request failed: 原始上游错误: {details}')
        b=Agent(Config(ui_language='en',max_context_chars=1000),client=Fail())
        self.assertIn('Context budget exceeded',b.run('test'))
        self.assertIn('/continue',b.last_run['continue_hint'])

    def test_all_english_help_lines_are_translated_without_changing_command_names(self):
        from termux_agent.cli import HELP
        english=help_text(HELP,'en')
        self.assertNotRegex(english,r'[\u3400-\u9fff]')
        self.assertEqual(re.findall(r'^/\S+',HELP,re.M),re.findall(r'^/\S+',english,re.M))

    def test_english_table_and_context_fit_mobile_width_and_preserve_data(self):
        rows=[{'id':'abcdef123456','title':'中文是会话数据','api':'cloud','model':'model',
               'updated':'2026-10-30 10:00','messages':42,'directory':'/some/中文路径'}]
        original=copy.deepcopy(rows)
        for width in (32,48,80,120):
            lines=session_table(rows,width,language='en')
            self.assertNotRegex(lines[0],r'[\u3400-\u9fff]')
            self.assertTrue(all(_cell_width(line)<=width-1 for line in lines))
        self.assertEqual(rows,original)
        self.assertIn('中文是会话数据',session_table(rows,180,language='en')[1])
        status=Agent(Config(ui_language='en',context_window_tokens=32768),client=fixtures.FakeClient()).context_status()
        self.assertIn('reserve',format_context_status(status,'en'))
        self.assertNotRegex(format_context_status(status,'en'),r'[\u3400-\u9fff]')

    def test_catalog_formats_values_after_translation_without_rewriting_them(self):
        self.assertEqual(text('Loaded {v0} into this session.','zh',v0='Session deleted.'),
                         '已将 Session deleted. 加载到当前会话。')
        self.assertEqual(text('LLM 请求失败：{v0}','en',v0='用户正文 {v0}'),
                         'LLM request failed: 用户正文 {v0}')


if __name__=='__main__':unittest.main()
