"""Tests for progressive /help, /, spellcheck, /model, /api, /skill, and /set CLI UX."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

try:
    import httpx
except ImportError:
    httpx = None

from termux_agent.config import Config
from termux_agent.i18n import help_text
from termux_agent.terminal_ui import COMMANDS, SUBCOMMANDS, Completer


CONFIG_TEXT = """
default_api = "local"
max_context_chars = 50000

[apis.local]
base_url = "http://127.0.0.1:9/v1"
model = "local-default"
api_key_env = "LOCAL_KEY"
kind = "local"
token_count_mode = "off"

[apis.cloud]
base_url = "https://cloud.example/v1"
model = "cloud-default"
api_key_env = "CLOUD_KEY"
kind = "cloud"
token_count_mode = "off"
"""


class FakeTtyOut(io.StringIO):
    def __init__(self, tty: bool):
        super().__init__()
        self._tty = tty

    def isatty(self):
        return self._tty


class CliUxTests(unittest.TestCase):
    def _run_repl(self, prompts, *, tty=False, language='zh', config_text=CONFIG_TEXT,
                  model_respond=None, skills_dir=None, argv_extra=()):
        from termux_agent import cli
        from termux_agent.client import Client
        from termux_agent.skills import SkillRegistry

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg_path = root / 'config.toml'
            prefix = f'ui_language = "{language}"\n' if language else ''
            cfg_path.write_text(prefix + config_text, encoding='utf-8')
            agents = []
            prompts_iter = iter(prompts)
            input_prompts = []

            class FakeAgent:
                def __init__(self, config, **kwargs):
                    self.config = config
                    self.active_skills = {}
                    self.messages = [{'role': 'system', 'content': 'rules'}]
                    self.base_system = 'rules'
                    self.system = 'rules'
                    self.last_run = {'status': 'idle', 'stop_reason': 'idle'}
                    self.prompts = []
                    self.client = type('C', (), {'close': lambda _s: None})()
                    agents.append(self)

                def get_active_skills(self):
                    return dict(self.active_skills)

                def load_skill(self, name, content):
                    self.active_skills[name] = content

                def unload_skill(self, name):
                    self.active_skills.pop(name, None)

                def clear_skills(self):
                    self.active_skills.clear()

                def context_status(self):
                    return {
                        'state': '正常', 'chars': 100, 'limit': 50000,
                        'threshold': 0.75, 'auto_compact': True,
                        'tokens': {'prompt_tokens': None, 'context_window_tokens': None,
                                   'exact': False, 'output_reserve_tokens': 0},
                    }

                def _resumable(self):
                    return False

                def run(self, text, auto_continue=None):
                    self.prompts.append(text)
                    return 'ok'

            def fake_input(prompt=''):
                input_prompts.append(prompt)
                return next(prompts_iter)

            def make_client(cfg):
                if httpx is None or model_respond is None:
                    raise AssertionError('unexpected Client call')
                return Client(cfg, http_client=httpx.Client(transport=httpx.MockTransport(model_respond)))

            out = FakeTtyOut(tty)
            prev_cwd = Path.cwd()
            skill_registry = SkillRegistry([skills_dir]) if skills_dir else SkillRegistry([])
            try:
                with patch.dict(os.environ, {'HOME': str(root), 'LOCAL_KEY': 'local-sec', 'CLOUD_KEY': 'cloud-sec'}), \
                     patch.object(cli, 'Agent', FakeAgent), \
                     patch.object(cli, 'Client', side_effect=make_client), \
                     patch.object(cli, 'SkillRegistry', return_value=skill_registry), \
                     patch.object(cli.sys, 'argv', ['ta', '--no-session', '--no-agent', '--config', str(cfg_path), '--directory', str(root), *argv_extra]), \
                     patch.object(cli.sys.stdin, 'isatty', return_value=tty), \
                     patch.object(cli.sys, 'stdout', out), \
                     patch.object(cli, '_readline', None), \
                     patch('builtins.input', side_effect=fake_input):
                    cli.main()
            finally:
                os.chdir(prev_cwd)
            return out.getvalue(), agents, input_prompts

    def test_quick_menu_and_filtered_help_sections_work_in_zh_and_en(self):
        from termux_agent.cli import HELP
        en_help = help_text(HELP, 'en')
        self.assertNotRegex(en_help, r'[\u3400-\u9fff]')
        self.assertEqual(re.findall(r'^/\S+', HELP, re.M), re.findall(r'^/\S+', en_help, re.M))

        out_zh, _, _ = self._run_repl(['/', '/help api', '/help compact', '/help nosuch', '/exit'])
        self.assertIn('常用命令速查', out_zh)
        self.assertIn('[API 与模型]', out_zh)
        self.assertNotIn('[会话管理]', out_zh.split('[API 与模型]', 1)[1].split('/compact', 1)[0])
        self.assertIn('/compact task [N]', out_zh)
        self.assertIn('未找到匹配的帮助主题或命令', out_zh)

        out_en, _, _ = self._run_repl(['/', '/help model', '/help nosuch', '/exit'], language='en')
        self.assertIn('Quick commands', out_en)
        self.assertIn('[API & Models]', out_en)
        self.assertIn('No matching help topic or command', out_en)
        self.assertNotRegex(out_en, r'[\u3400-\u9fff]')

    def test_spellcheck_suggests_close_commands_without_blocking_file_paths(self):
        out_zh, agents, _ = self._run_repl(['/resum', '/modle', '/xyznohit', '/help.txt', '/exit'])
        self.assertIn('/resume', out_zh)
        self.assertIn('/model', out_zh)
        self.assertIn('未知命令 /xyznohit', out_zh)
        # Dotted filename goes through path_value (No such file), not unknown-command handler.
        self.assertNotIn('未知命令 /help.txt', out_zh)
        self.assertEqual(agents[-1].prompts, [])

        out_en, _, _ = self._run_repl(['/resum', '/exit'], language='en')
        self.assertIn('did you mean: /resume', out_en)
        self.assertNotRegex(out_en, r'[\u3400-\u9fff]')

    @unittest.skipIf(httpx is None, 'httpx required for /model tests')
    def test_model_command_lists_picks_and_cancels_in_tty_and_nontty(self):
        def respond(request):
            if request.url.host == '127.0.0.1':
                return httpx.Response(200, json={'data': [{'id': 'local-a'}, {'id': 'local-b'}]})
            return httpx.Response(200, json={'data': [{'id': 'cloud-x'}, {'id': 'cloud-y'}]})

        # Non-TTY: /model lists without consuming next prompt; /model <id|N> switches directly.
        out_nontty, agents_nontty, _ = self._run_repl(
            ['/model', '/model 2', '/model cloud-y cloud', 'after-switch', '/exit'],
            tty=False, model_respond=respond,
        )
        self.assertIn('local-a', out_nontty)
        self.assertEqual(agents_nontty[1].config.model, 'local-b')
        self.assertEqual(agents_nontty[-1].config.api_profile, 'cloud')
        self.assertEqual(agents_nontty[-1].config.model, 'cloud-y')
        self.assertEqual(agents_nontty[-1].prompts, ['after-switch'])

        # Interactive TTY: bare /model and /model list cloud prompt for ordinal pick or empty cancel.
        out_tty, agents_tty, _ = self._run_repl(
            ['/model', '1', '/model list cloud', '', '/exit'],
            tty=True, model_respond=respond,
        )
        self.assertEqual(agents_tty[-1].config.model, 'local-a')
        self.assertIn('已取消模型选择', out_tty)

    @unittest.skipIf(httpx is None, 'httpx required for /api TTY picker test')
    def test_api_tty_picker_switches_by_ordinal_while_nontty_keeps_json(self):
        out_nontty, agents_nontty, _ = self._run_repl(['/api', '/exit'], tty=False)
        self.assertIn('"profiles"', out_nontty)
        self.assertEqual(agents_nontty[-1].config.api_profile, 'local')

        out_tty, agents_tty, _ = self._run_repl(['/api', '2', '/api', '', '/exit'], tty=True)
        self.assertIn('可用 API 配置', out_tty)
        self.assertEqual(agents_tty[-1].config.api_profile, 'cloud')
        self.assertIn('已取消 API 选择', out_tty)

    def test_skill_tty_toggle_and_ordinal_shortcuts(self):
        with tempfile.TemporaryDirectory() as sd:
            skill_dir = Path(sd) / 'alpha'
            skill_dir.mkdir()
            (skill_dir / 'SKILL.md').write_text('---\nname: alpha\ndescription: alpha desc\n---\nALPHA_BODY')

            out_tty, agents_tty, _ = self._run_repl(
                ['/skill', '1', '/skill', '', '/skill 1', '/skill load 1', '/skill unload 1', '/exit'],
                tty=True, skills_dir=Path(sd),
            )
            self.assertIn('alpha desc', out_tty)
            self.assertIn('已将 alpha 加载到当前会话', out_tty)
            self.assertIn('已卸载 alpha', out_tty)
            self.assertIn('已取消技能操作', out_tty)
            self.assertEqual(agents_tty[-1].active_skills, {})

            out_nontty, agents_nontty, _ = self._run_repl(
                ['/skill', '/skill load 1', '/skill info 1', '/exit'],
                tty=False, skills_dir=Path(sd),
            )
            self.assertIn('"name": "alpha"', out_nontty)
            self.assertEqual(list(agents_nontty[-1].active_skills), ['alpha'])

    def test_set_command_and_auto_debug_shortcuts(self):
        out_nontty, agents_nontty, _ = self._run_repl(
            ['/set', '/set auto on', '/set steps 24', '/set silent off', '/set debug on',
             '/auto 12', '/debug on', '/debug off', '/debug bogus', '/set', '/exit'],
            tty=False,
        )
        cfg = agents_nontty[-1].config
        self.assertTrue(cfg.auto_continue)
        self.assertEqual(cfg.max_steps, 12)
        self.assertFalse(cfg.silent_writes)
        self.assertIn('"debug": true', out_nontty)
        self.assertIn('调试显示：on', out_nontty)
        self.assertIn('调试显示：off', out_nontty)
        self.assertIn('用法：/debug [on|off]', out_nontty)

        out_tty, agents_tty, _ = self._run_repl(
            ['/set', '1', '/set', '2', '42', '/set', '', '/exit'],
            tty=True, language='en',
        )
        self.assertTrue(agents_tty[-1].config.auto_continue)
        self.assertEqual(agents_tty[-1].config.max_steps, 42)
        self.assertIn('Settings unchanged', out_tty)
        self.assertNotRegex(out_tty, r'[\u3400-\u9fff]')

    def test_completer_covers_model_set_and_help_subcommands(self):
        class Buf:
            def __init__(self, line, begin):
                self.line = line
                self.begin = begin

            def get_line_buffer(self):
                return self.line

            def get_begidx(self):
                return self.begin

        completer = Completer(
            None, lambda: ['local', 'cloud'], lambda: ['alpha'], lambda: [],
            lambda: ['abcdef123456'], lambda: Path('.'),
            model_choices=lambda: ['gpt-4o', 'model with spaces', '1', '2'],
        )
        self.assertIn('/model', COMMANDS)
        self.assertIn('/set', COMMANDS)
        self.assertIn('steps', SUBCOMMANDS['/set'])
        self.assertEqual(completer.matches('/model ', 7, ''), ["'model with spaces'", '1', '2', 'gpt-4o', 'list'])
        self.assertEqual(completer.matches('/model list c', 12, 'c'), ['cloud'])
        self.assertEqual(completer.matches('/model 1 l', 9, 'l'), ['local'])
        self.assertEqual(completer.matches('/set a', 5, 'a'), ['auto'])
        self.assertEqual(completer.matches('/set silent o', 12, 'o'), ['off', 'on'])
        self.assertIn('task', completer.matches('/help t', 6, 't'))

    def test_history_filter_excludes_quick_menu_and_unknown_slash_commands(self):
        from termux_agent.cli import _history_input

        for excluded in ('/', '/   ', '/resum', '/resum arg', '/model', '/set'):
            self.assertFalse(_history_input(excluded), f'should exclude {excluded!r}')
        for kept in ('normal task', '/help.txt', '/nonexistent/file.txt', '~/notes.txt', '"/resum"'):
            self.assertTrue(_history_input(kept), f'should keep {kept!r}')

    def test_numeric_skill_name_takes_precedence_over_ordinal(self):
        with tempfile.TemporaryDirectory() as sd:
            for name, body in (('2', 'NUMERIC_BODY'), ('alpha', 'ALPHA_BODY')):
                skill_dir = Path(sd) / name
                skill_dir.mkdir()
                (skill_dir / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: {name} desc\n---\n{body}')

            out, agents, _ = self._run_repl(
                ['/skill load 2', '/skill info 2', '/skill unload 2', '/skill list', '/skill load 2',
                 '/skill unload 2', '/skill 1', '/exit'],
                tty=False, skills_dir=Path(sd),
            )
            self.assertIn('已将 2 加载到当前会话', out)
            self.assertIn('"name": "2"', out)
            self.assertIn('已卸载 2', out)
            self.assertEqual(list(agents[-1].active_skills), ['2'])
            self.assertNotIn('alpha', agents[-1].active_skills)

            out_keep, agents_keep, _ = self._run_repl(
                ['/skill load 2', '/skill 2', '/skill load 2', '/exit'],
                tty=False, skills_dir=Path(sd),
            )
            self.assertEqual(list(agents_keep[-1].active_skills), ['2'])
            self.assertNotIn('alpha', agents_keep[-1].active_skills)


if __name__ == '__main__':
    unittest.main()
