"""Compact session lists use terminal cell widths, not Python character counts."""
import copy
import io
import unicodedata
import unittest
from unittest.mock import patch

from termux_agent.terminal_ui import Columns, _table_cell, session_table


def cells(text):
    return sum(0 if unicodedata.combining(c) else
               2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1 for c in text)


class SessionTableTests(unittest.TestCase):
    def row(self, **overrides):
        return dict({'id': 'abcdef123456', 'updated': '2026-10-20 16:56:23',
                     'title': '稿件校对', 'api': 'cloud', 'model': 'deepseek-v4-flash',
                     'messages': 321, 'directory': '/home/me/work/project'}, **overrides)

    def test_one_line_per_session_with_requested_field_order(self):
        lines = session_table([self.row()], 120, 'abcdef123456', '/home/me')
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith('序号'))
        self.assertRegex(lines[1], r'^1\s+')
        self.assertIn('最后会话时间', lines[0])
        self.assertLess(lines[0].index('ID'), lines[0].index('title'))
        self.assertLess(lines[0].index('title'), lines[0].index('API'))
        self.assertLess(lines[0].index('API'), lines[0].index('消息数'))
        self.assertLess(lines[0].index('消息数'), lines[0].index('工作路径'))
        line = lines[1]
        self.assertIn('2026-10-20 16:56', line)
        self.assertIn('*abcdef123456', line)
        self.assertLess(line.index('16:56'), line.index('abcdef123456'))
        self.assertLess(line.index('abcdef123456'), line.index('稿件校对'))
        self.assertLess(line.index('稿件校对'), line.index('cloud'))
        self.assertLess(line.index('cloud'), line.index('321'))
        self.assertLess(line.index('321'), line.index('~/work/project'))
        self.assertIn('cloud / deepseek-v4-flash', line)
        self.assertIn('*abcdef123456', line)

    def test_numbered_first_column_does_not_mutate_rows(self):
        rows = [self.row(), self.row(id='123456abcdef', title='第二会话', api='mimo',
                                     model='mimo-v2.6-pro', messages=0,
                                     directory='/home/me/second')]
        before = copy.deepcopy(rows)
        lines = session_table(rows, 120, rows[0]['id'], '/home/me')
        self.assertTrue(lines[0].startswith('序号'))
        self.assertRegex(lines[1], r'^1\s+')
        self.assertRegex(lines[2], r'^2\s+')
        self.assertIn('abcdef123456', lines[1])
        self.assertIn('123456abcdef', lines[2])
        self.assertIn('*abcdef123456', lines[1])
        self.assertNotIn('*123456abcdef', lines[2])
        for value in ('2026-10-20 16:56', '稿件校对', 'cloud / deepseek-v4-flash',
                      '321', '~/work/project'):
            self.assertIn(value, lines[1])
        for value in ('第二会话', 'mimo / mimo-v2.6-pro', '0', '~/second'):
            self.assertIn(value, lines[2])
        self.assertEqual(rows, before)

    def test_long_chinese_titles_profiles_and_paths_fit_without_changing_data(self):
        rows = [self.row(title='很长的中文标题' * 20, api='cloud-provider-long-name', messages=1234567,
                         directory='/home/me/' + '深层目录/' * 20 + 'report'),
                self.row(id='123456abcdef', api='mimo')]
        before = copy.deepcopy(rows)
        for width in (32, 48, 64, 80, 120):
            with self.subTest(width=width):
                lines = session_table(rows, width, rows[0]['id'], '/home/me')
                self.assertEqual(len(lines), 3)
                for line in lines:
                    self.assertLessEqual(cells(line), width - 1, line)
                    self.assertNotIn('\n', line)
                self.assertTrue(lines[0].startswith('序号' if width >= 80 else '#'))
                self.assertRegex(lines[1], r'^1\s+')
                self.assertRegex(lines[2], r'^2\s+')
                self.assertIn('abcdef123456', lines[1])
                self.assertIn('123456abcdef', lines[2])
                self.assertIn('1234567', lines[1])
                self.assertIn('…', lines[1])
        self.assertEqual(rows, before)
        self.assertTrue(session_table(rows[:1], 120, home='/home/me')[1].endswith('report'))

    def test_controls_are_escaped_and_secrets_redacted_before_sizing(self):
        row = self.row(title='SECRET\ntext\x1b[31m', api='SECRET-cloud', model='SECRET-model', directory='/home/me/SECRET/project')
        lines = session_table([row], 64, home='/home/me', redact=lambda s: s.replace('SECRET', '[REDACTED VALUE]'))
        for line in lines:
            self.assertNotIn('SECRET', line)
            self.assertNotIn('\x1b', line)
            self.assertNotIn('\n', line)
            self.assertLessEqual(cells(line), 63)

    def test_table_uses_full_width_without_per_row_role_or_current_time(self):
        out = io.StringIO()
        columns = Columns(out, width=lambda: 80, clock=lambda: self.fail('must not stamp session rows'))
        columns.sessions([self.row(), self.row(id='123456abcdef', api='mimo')], home='/home/me')
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn('10-20 16:56', lines[1])
        self.assertIn('mimo', lines[2])
        self.assertTrue(all('│' not in line and cells(line) <= 79 for line in lines))

    def test_path_home_boundary_and_combining_characters_are_preserved(self):
        line = session_table([self.row(directory='/home/me2/project')], 120, home='/home/me')[1]
        self.assertIn('/home/me2/project', line)
        self.assertNotIn('~', line)
        clipped = _table_cell('0123456789Café cafe\u0301', 7, middle=True)
        self.assertTrue(clipped.rstrip().endswith('e\u0301'))
        self.assertLessEqual(cells(clipped), 7)

    def test_common_models_are_visible_at_80_columns_and_missing_model_is_supported(self):
        rows = [self.row(messages=0), self.row(id='123456abcdef', api='mimo', model='mimo-v2.6-pro')]
        lines = session_table(rows, 80, home='/home/me')
        self.assertIn('API / 模型', lines[0])
        self.assertIn('cloud / deepseek-v4-flash', lines[1])
        self.assertRegex(lines[1], r'deepseek-v4-flash\s+0\s+')
        self.assertIn('mimo / mimo-v2.6-pro', lines[2])
        self.assertTrue(all(cells(line) <= 79 for line in lines))
        line = session_table([self.row(model=None, messages=None)], 80, home='/home/me')[1]
        self.assertIn('cloud', line)
        self.assertNotIn('None', line)
        self.assertRegex(line, r'cloud\s+-\s+~/work/project$')

    def test_80_column_overflow_keeps_chinese_title_readable_without_losing_ids_or_counts(self):
        row = self.row(title='稿件校对与会议讨论', model='deepseek-v4.1-flash-reasoner', messages=98765)
        lines = session_table([row], 80, 'abcdef123456', '/home/me')
        line = lines[1]
        self.assertIn('稿件…', line)
        self.assertIn('*abcdef123456', line)
        self.assertRegex(line, r'\s98765\s')
        self.assertEqual(cells(line), 79)
        self.assertTrue(all(cells(item) <= 79 for item in lines))

    def test_help_includes_simplified_chinese_for_existing_command_syntax(self):
        from termux_agent.cli import HELP
        self.assertIn('显示命令帮助', HELP)
        self.assertIn('API / 模型', HELP)
        self.assertIn('无需调用模型', HELP)
        for command in ('/sessions find TEXT', '/session rename-id ID NAME', '/title', '/sub', '/api'):
            self.assertIn(command, HELP)

    def test_command_line_help_has_chinese_option_descriptions_without_starting_agent(self):
        from termux_agent import cli
        out = io.StringIO()
        with patch.object(cli.sys, 'argv', ['ta', '--help']), patch.object(cli.sys, 'stdout', out), \
                patch.object(cli, 'Agent') as agent:
            with self.assertRaises(SystemExit) as exited:
                cli.main()
        self.assertEqual(exited.exception.code, 0)
        agent.assert_not_called()
        self.assertIn('轻量原生 Termux', out.getvalue())
        self.assertIn('TOML 配置文件路径', out.getvalue())
        self.assertIn('--api', out.getvalue())
