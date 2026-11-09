"""Regression tests for legacy compaction-chain presentation."""
import copy
import unittest

from termux_agent.session_listing import collapse_compaction_rows


SCOPE = ('https://api.example/v1', 'model', 'chat_completions', '/work')


def row(identifier, title=None, *, archive=None, updated=None, messages=2,
        scope=SCOPE, compaction_count=0):
    value = {
        'id': identifier,
        'title': title or identifier,
        'updated': updated or '2026-11-08 12:00:00',
        'api': 'cloud',
        'model': 'model',
        'directory': '/work',
        'messages': messages,
        '_scope_identity': scope,
    }
    if archive is not None:
        value['_archive_session_id'] = archive
    if compaction_count:
        value['compaction_count'] = compaction_count
    return value


class CompactionListingTests(unittest.TestCase):
    def test_collapses_linear_chain_at_tip_without_mutating_rows(self):
        rows = [
            row('cccccccccccc', 'Topic · compact task', archive='bbbbbbbbbbbb',
                updated='2026-11-08 12:03:00', messages=3, compaction_count=2),
            row('dddddddddddd', 'Manual fork', updated='2026-11-08 12:02:00'),
            row('bbbbbbbbbbbb', 'Topic · compact task', archive='aaaaaaaaaaaa',
                updated='2026-11-08 12:01:00'),
            row('aaaaaaaaaaaa', 'Topic', updated='2026-11-08 12:00:00'),
        ]
        before = copy.deepcopy(rows)

        collapsed = collapse_compaction_rows(rows)

        self.assertEqual(rows, before)
        self.assertEqual([item['id'] for item in collapsed],
                         ['cccccccccccc', 'dddddddddddd'])
        self.assertIsNot(collapsed[1], rows[1])
        self.assertEqual(collapsed[0]['title'], 'Topic')
        self.assertEqual(collapsed[0]['messages'], 3)
        self.assertEqual(collapsed[0]['archived_ids'],
                         ['aaaaaaaaaaaa', 'bbbbbbbbbbbb'])
        self.assertEqual(collapsed[0]['compaction_count'], 4)
        for item in collapsed:
            self.assertFalse(any(key.startswith('_') for key in item))

    def test_ambiguous_fork_component_stays_separate(self):
        rows = [
            row('cccccccccccc', 'Fork C', archive='aaaaaaaaaaaa'),
            row('bbbbbbbbbbbb', 'Fork B', archive='aaaaaaaaaaaa'),
            row('aaaaaaaaaaaa', 'Root'),
        ]

        collapsed = collapse_compaction_rows(rows)

        self.assertEqual([item['id'] for item in collapsed],
                         ['cccccccccccc', 'bbbbbbbbbbbb', 'aaaaaaaaaaaa'])
        self.assertTrue(all('archived_ids' not in item for item in collapsed))

    def test_cycle_and_identity_mismatch_are_not_collapsed(self):
        rows = [
            row('bbbbbbbbbbbb', 'Cycle B', archive='aaaaaaaaaaaa'),
            row('aaaaaaaaaaaa', 'Cycle A', archive='bbbbbbbbbbbb'),
            row('dddddddddddd', 'Other scope', archive='cccccccccccc',
                scope=('https://other/v1', 'model', 'chat_completions', '/work')),
            row('cccccccccccc', 'Different identity'),
        ]

        collapsed = collapse_compaction_rows(rows)

        self.assertEqual([item['id'] for item in collapsed],
                         ['bbbbbbbbbbbb', 'aaaaaaaaaaaa', 'dddddddddddd',
                          'cccccccccccc'])

    def test_dangling_parent_does_not_hide_the_available_child_chain(self):
        rows = [
            row('cccccccccccc', 'Topic · compact task', archive='bbbbbbbbbbbb'),
            row('bbbbbbbbbbbb', 'Topic · compact task', archive='aaaaaaaaaaaa'),
        ]

        collapsed = collapse_compaction_rows(rows)

        self.assertEqual(len(collapsed), 1)
        self.assertEqual(collapsed[0]['id'], 'cccccccccccc')
        self.assertEqual(collapsed[0]['title'], 'Topic · compact task')
        self.assertEqual(collapsed[0]['archived_ids'], ['bbbbbbbbbbbb'])
        self.assertEqual(collapsed[0]['compaction_count'], 1)

    def test_generated_truncated_title_uses_root_and_manual_tip_title_is_kept(self):
        root_title = 'R' * 120
        generated = root_title[:95] + ' · compact task'
        generated_rows = [
            row('bbbbbbbbbbbb', generated, archive='aaaaaaaaaaaa'),
            row('aaaaaaaaaaaa', root_title),
        ]
        renamed_rows = [
            row('bbbbbbbbbbbb', 'Manually renamed', archive='aaaaaaaaaaaa'),
            row('aaaaaaaaaaaa', 'Original'),
        ]

        self.assertEqual(collapse_compaction_rows(generated_rows)[0]['title'],
                         root_title)
        self.assertEqual(collapse_compaction_rows(renamed_rows)[0]['title'],
                         'Manually renamed')

    def test_standalone_rows_keep_public_shape_and_zero_count_is_omitted(self):
        plain = row('aaaaaaaaaaaa', 'Plain')
        archived = row('bbbbbbbbbbbb', 'In-memory archive', compaction_count=2)

        collapsed = collapse_compaction_rows([plain, archived])

        self.assertEqual(collapsed[0], {
            key: value for key, value in plain.items()
            if not key.startswith('_') and key != 'compaction_count'
        })
        self.assertEqual(collapsed[1]['compaction_count'], 2)
        self.assertNotIn('archived_ids', collapsed[1])


if __name__ == '__main__':
    unittest.main()
