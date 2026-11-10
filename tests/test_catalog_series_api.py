import sqlite3
import tempfile
import unittest
from unittest import mock

from flask import Flask

from blueprints.library import routes


class CatalogSeriesApiTest(unittest.TestCase):
    """/api/catalog/series : catalogue paginé pour l'extension Mihon / Tachiyomi."""

    def setUp(self):
        self.db = tempfile.NamedTemporaryFile(suffix='.db')
        conn = sqlite3.connect(self.db.name)
        conn.execute('''CREATE TABLE series (id INTEGER PRIMARY KEY, title TEXT, library_id INTEGER,
            added_at TEXT, local_cover_path TEXT, bedetheque_cover_path TEXT, komga_cover_path TEXT,
            manual_summary TEXT, bedetheque_description TEXT, local_summary TEXT,
            manual_genre TEXT, bedetheque_genre TEXT, local_genre TEXT,
            manual_author TEXT, bedetheque_scenaristes TEXT, local_author TEXT,
            bedetheque_dessinateurs TEXT, manual_status TEXT, bedetheque_status TEXT, is_oneshot INTEGER)''')
        conn.execute('CREATE TABLE volumes (id INTEGER PRIMARY KEY, series_id INTEGER, filepath TEXT)')
        rows = [
            (1, 'Étoile du Nord', 1, '2026-01-01', 'covers/a.jpg', None, None, None, 'Résumé A', None,
             None, 'Aventure', None, None, 'Durand', None, 'Martin', None, 'Série en cours', 0),
            (2, 'Blake et Mortimer', 1, '2026-03-01', None, 'covers/b.jpg', None, 'Manuel', None, None,
             None, 'Horreur', None, None, 'Jacobs', None, None, None, None, 0),
            (3, 'Astérix', 2, '2026-02-01', None, None, None, None, None, None,
             None, None, None, 'Goscinny', None, None, 'Uderzo', 'Série finie', None, 1),
            (4, 'Vide', 1, '2026-04-01', None, None, None, None, None, None,
             None, None, None, None, None, None, None, None, None, 0),
        ]
        conn.executemany('INSERT INTO series VALUES (' + ','.join('?' * 20) + ')', rows)
        conn.executemany('INSERT INTO volumes VALUES (?, ?, ?)', [
            (10, 1, '/BD/a1.cbz'), (11, 1, '/BD/a2.cbz'), (12, 2, '/BD/b1.cbz'),
            (13, 3, '/BD/c1.cbz'), (14, 4, None), (15, 4, ''), (20, 1, '/BD/a3.cbz'),
        ])
        conn.commit()
        conn.close()
        self.app = Flask(__name__)
        self.app.config['DATABASE'] = self.db.name
        patch = mock.patch.object(routes, 'get_db_connection', self._connect)
        patch.start()
        self.addCleanup(patch.stop)
        self.restrictions = (None, set())
        patch = mock.patch('blueprints.auth.roles.current_restrictions', lambda: self.restrictions)
        patch.start()
        self.addCleanup(patch.stop)

    def tearDown(self):
        self.db.close()

    def _connect(self):
        conn = sqlite3.connect(self.db.name)
        conn.row_factory = sqlite3.Row
        return conn

    def _get(self, qs=''):
        with self.app.test_request_context('/api/catalog/series' + qs):
            return routes.get_catalog_series().get_json()

    def test_only_series_with_files_sorted_by_title_without_accents(self):
        data = self._get()
        self.assertEqual([s['title'] for s in data['series']], ['Astérix', 'Blake et Mortimer', 'Étoile du Nord'])
        self.assertEqual(data['total'], 3)
        self.assertFalse(data['has_more'])

    def test_fields_follow_detail_priorities(self):
        by_id = {s['id']: s for s in self._get()['series']}
        self.assertEqual(by_id[1]['cover'], '/covers/a.jpg')
        self.assertEqual(by_id[2]['cover'], '/covers/b.jpg')
        self.assertIsNone(by_id[3]['cover'])
        self.assertEqual(by_id[2]['summary'], 'Manuel')
        self.assertEqual(by_id[1]['author'], 'Durand')
        self.assertEqual(by_id[3]['author'], 'Goscinny')
        self.assertEqual(by_id[1]['artist'], 'Martin')
        self.assertEqual(by_id[1]['owned_volumes'], 3)
        self.assertTrue(by_id[3]['is_oneshot'])

    def test_pagination(self):
        first = self._get('?size=2&page=1')
        second = self._get('?size=2&page=2')
        self.assertTrue(first['has_more'])
        self.assertFalse(second['has_more'])
        self.assertEqual(len(first['series']) + len(second['series']), 3)

    def test_search_title_or_author_ignoring_accents(self):
        self.assertEqual([s['id'] for s in self._get('?q=etoile')['series']], [1])
        self.assertEqual([s['id'] for s in self._get('?q=JACOBS')['series']], [2])

    def test_sorts(self):
        self.assertEqual([s['id'] for s in self._get('?sort=added')['series']], [2, 3, 1])
        self.assertEqual([s['id'] for s in self._get('?sort=updated')['series']], [1, 3, 2])

    def test_reader_restrictions_and_library_filter(self):
        self.restrictions = ({1}, {'horreur'})
        self.assertEqual([s['id'] for s in self._get()['series']], [1])
        self.restrictions = (None, set())
        self.assertEqual([s['id'] for s in self._get('?library_id=2')['series']], [3])


if __name__ == '__main__':
    unittest.main()
