"""Compact session lists use terminal cell widths, not Python character counts."""
import copy
import io
import unicodedata
import unittest

from termux_agent.terminal_ui import Columns, _table_cell, session_table


def cells(text):
    return sum(0 if unicodedata.combining(c) else
               2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1 for c in text)


class SessionTableTests(unittest.TestCase):
    def row(self, **overrides):
        return dict({'id': 'abcdef123456', 'updated': '2026-10-20 16:56:23',
                     'title': '稿件校对', 'api': 'cloud', 'directory': '/home/me/work/project'}, **overrides)

    def test_one_line_per_session_with_requested_field_order(self):
        lines = session_table([self.row()], 120, 'abcdef123456', '/home/me')
        self.assertEqual(len(lines), 2)
        self.assertIn('最后会话时间', lines[0])
        self.assertLess(lines[0].index('ID'), lines[0].index('title'))
        self.assertLess(lines[0].index('title'), lines[0].index('API'))
        self.assertLess(lines[0].index('API'), lines[0].index('工作路径'))
        line = lines[1]
        self.assertIn('2026-10-20 16:56', line)
        self.assertIn('*abcdef123456', line)
        self.assertLess(line.index('16:56'), line.index('abcdef123456'))
        self.assertLess(line.index('abcdef123456'), line.index('稿件校对'))
        self.assertLess(line.index('稿件校对'), line.index('cloud'))
        self.assertLess(line.index('cloud'), line.index('~/work/project'))

    def test_long_chinese_titles_profiles_and_paths_fit_without_changing_data(self):
        rows = [self.row(title='很长的中文标题' * 20, api='cloud-provider-long-name',
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
                self.assertIn('abcdef123456', lines[1])
                self.assertIn('123456abcdef', lines[2])
                self.assertIn('…', lines[1])
        self.assertEqual(rows, before)
        self.assertTrue(session_table(rows[:1], 80, home='/home/me')[1].endswith('report'))

    def test_controls_are_escaped_and_secrets_redacted_before_sizing(self):
        row = self.row(title='SECRET\ntext\x1b[31m', api='SECRET-cloud', directory='/home/me/SECRET/project')
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
