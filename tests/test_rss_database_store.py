import json
import os
import sqlite3
import tempfile
import unittest

from blueprints.ebdz import routes


class RssDatabaseStoreTest(unittest.TestCase):
    """Le cache RSS est en base SQLite (une ligne par release), partagé par le site et l'application."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'rss_entries_cache.json')

    def test_old_json_cache_is_imported_once_then_set_aside(self):
        legacy = {'feed': {'fetched_at': 5, 'entries': [{'title': 'A', 'link': 'l1', 'date': '2026-10-02'},
                                                        {'title': 'B', 'link': 'l2', 'date': '2026-10-01'}],
                           'annotated_entries': [{'title': 'A', 'link': 'l1', 'series_id': 7}]}}
        with open(self.path, 'w', encoding='utf-8') as handle:
            json.dump(legacy, handle)
        cache = routes._load_rss_persistent_cache(self.path)
        self.assertEqual(cache, legacy)
        self.assertFalse(os.path.exists(self.path))
        self.assertTrue(os.path.exists(self.path + '.migrated'))
        conn = sqlite3.connect(os.path.splitext(self.path)[0] + '.db')
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM rss_entries').fetchone()[0], 2)
        self.assertEqual(routes._load_rss_persistent_cache(self.path), legacy)

    def test_update_rewrites_only_changed_feeds_and_keeps_order(self):
        routes._update_rss_persistent_cache(self.path, lambda c: c.update({
            'one': {'fetched_at': 1, 'entries': [{'title': 'X', 'link': 'x'}, {'title': 'Y', 'link': 'y'}]},
            'two': {'fetched_at': 2, 'entries': [{'title': 'Z', 'link': 'z'}]}}))
        routes._update_rss_persistent_cache(self.path, lambda c: c['two'].update({'fetched_at': 3}))
        cache = routes._load_rss_persistent_cache(self.path)
        self.assertEqual([e['title'] for e in cache['one']['entries']], ['X', 'Y'])
        self.assertEqual(cache['two']['fetched_at'], 3)
        self.assertNotIn('annotated_entries', cache['one'])

    def test_removed_feed_disappears(self):
        routes._update_rss_persistent_cache(self.path, lambda c: c.update({'a': {'fetched_at': 1, 'entries': []}}))
        routes._update_rss_persistent_cache(self.path, lambda c: c.pop('a'))
        self.assertEqual(routes._load_rss_persistent_cache(self.path), {})
