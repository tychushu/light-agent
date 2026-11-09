"""Persistence invariants for compaction versions and legacy presentation."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from termux_agent.sessions import SessionStore


def payload(content, parent=None):
    value = {
        'config': {'api_profile': 'cloud', 'model': 'fixture', 'base_url': 'https://fixture.invalid/v1'},
        'scope': {'directory': '/work'},
        'messages': [{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': content}],
    }
    if parent:
        value['branch'] = {'archive_session_id': parent}
    return value


class SessionArchiveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = SessionStore(self.root)
        self.addCleanup(self.store.close)
        self.session = 'aaaaaaaaaaaa'
        self.archive = 'archive-bbbbbbbbbbbb'
        self.original = payload('original record')
        self.store.save(self.session, 0, self.original, title='Original title')
        self.draft = {'content': 'partial response'}
        self.store.save_draft(self.session, 1, self.draft)

    def test_atomic_compaction_preserves_title_id_and_both_draft_views(self):
        reduced = payload('reduced record')
        self.assertEqual(self.store.compact(self.session, 1, self.archive, self.original, reduced), 2)
        self.assertEqual(self.store.ids(), [self.session])
        self.assertEqual(self.store.title(self.session), 'Original title')
        self.assertEqual(self.store.load(self.session), (2, reduced))
        self.assertEqual(self.store.draft(self.session)['content'], 'partial response')
        archive = self.store.load_archive(self.archive)
        self.assertEqual(archive['payload'], self.original)
        self.assertEqual(archive['revision'], 1)
        self.assertEqual(archive['draft']['content'], 'partial response')
        self.assertEqual(self.store.listing()[0]['compaction_count'], 1)
        exported = self.store.export(self.archive, 'json')
        self.assertEqual(json.loads(exported.read_text())['payload'], self.original)
        self.assertEqual(self.store.recall('original record')[0]['id'], self.archive)
        newer = 'archive-cccccccccccc'
        self.store.compact(self.session, 2, newer, reduced, payload('latest context'))
        self.assertEqual(self.store.ids(), [self.session])
        self.assertEqual(self.store.title(self.session), 'Original title')
        self.assertEqual([entry['id'] for entry in self.store.archives(self.session)], [newer, self.archive])
        self.assertEqual(self.store.load_archive(self.archive)['payload'], self.original)

    def test_archive_insert_failure_rolls_back_active_context_and_draft(self):
        self.store.db.execute("CREATE TRIGGER refuse_archive BEFORE INSERT ON session_archives "
                              "BEGIN SELECT RAISE(ABORT,'archive unavailable'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.compact(self.session, 1, self.archive, self.original, payload('uncommitted'))
        self.assertEqual(self.store.load(self.session), (1, self.original))
        self.assertEqual(self.store.draft(self.session)['content'], 'partial response')
        self.assertEqual(self.store.archives(self.session), [])

    def test_stale_compaction_keeps_newer_writer(self):
        other = SessionStore(self.root)
        self.addCleanup(other.close)
        other.save(self.session, 1, self.original)
        with self.assertRaisesRegex(ValueError, 'reload before compacting'):
            self.store.compact(self.session, 1, self.archive, self.original, payload('stale'))
        self.assertEqual(self.store.load(self.session), (2, self.original))
        self.assertEqual(self.store.archives(self.session), [])

    def test_legacy_filter_pagination_and_active_ancestor(self):
        tip = 'bbbbbbbbbbbb'
        self.store.save(tip, 0, payload('reduced', self.session), title='Original title · compact task')
        [row] = self.store.listing(1, query='original title')
        self.assertEqual((row['id'], row['title'], row['archived_ids']), (tip, 'Original title', [self.session]))
        self.assertEqual([row['id'] for row in self.store.listing(all_rows=True)], [tip, self.session])
        self.assertEqual([row['id'] for row in self.store.listing(active_id=self.session)], [tip, self.session])
