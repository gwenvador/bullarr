"""Avancement de lecture Komga affiché dans la bibliothèque (blueprints/komga/read_progress.py)."""
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from flask import Flask

from blueprints.library.scanner import LibraryScanner


def _page(books, last):
    response = mock.Mock(status_code=200)
    response.json.return_value = {'content': books, 'last': last}
    return response


BOOKS = [
    {'id': 'b1', 'seriesId': 'ks1', 'name': 'T1', 'media': {'pagesCount': 48}, 'readProgress': {'page': 48, 'completed': True, 'readDate': '2026-11-01T10:00:00Z'}},
    {'id': 'b2', 'seriesId': 'ks1', 'name': 'T2', 'media': {'pagesCount': 46}, 'readProgress': {'page': 12, 'completed': False, 'readDate': '2026-11-02T10:00:00Z'}},
    {'id': 'b3', 'seriesId': 'ks1', 'name': 'T3', 'media': {'pagesCount': 50}, 'readProgress': None},
    # Tome de Blake jamais rattaché (pas de komga_book_id) : relié par son nom de fichier.
    {'id': 'k9', 'seriesId': 'ks2', 'name': 'T1', 'media': {'pagesCount': 40}, 'readProgress': {'page': 5, 'completed': False, 'readDate': '2026-11-03T10:00:00Z'}},
]


class ReadProgressTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, 'bullarr.db')
        from config import Config
        Config._init_database(self.db)  # même schéma qu'au démarrage de l'app
        LibraryScanner(self.db)
        conn = sqlite3.connect(self.db)
        # Créée au démarrage par le module Bédéthèque, lue par la liste des séries.
        conn.execute('CREATE TABLE IF NOT EXISTS universes (id INTEGER PRIMARY KEY, name TEXT)')
        conn.execute("INSERT INTO libraries (id, name, path) VALUES (1, 'BD', '/BD')")
        conn.execute("INSERT INTO series (id, library_id, title, path, komga_series_id) VALUES (1, 1, 'Astérix', '/BD/Astérix', 'ks1')")
        conn.execute("INSERT INTO series (id, library_id, title, path, komga_series_id) VALUES (2, 1, 'Blake', '/BD/Blake', 'ks2')")
        for number, book in ((1, 'b1'), (2, 'b2'), (3, 'b3')):
            conn.execute('INSERT INTO volumes (series_id, volume_number, filename, filepath, komga_book_id, page_count) VALUES (1, ?, ?, ?, ?, 50)',
                         (number, f'T{number}.cbz', f'/BD/Astérix/T{number}.cbz', book))
        conn.execute("INSERT INTO volumes (series_id, volume_number, filename, filepath) VALUES (2, 1, 'T1.cbz', '/BD/Blake/T1.cbz')")
        conn.commit()
        conn.close()
        self.app = Flask(__name__)
        self.app.config['DATABASE'] = self.db

    def sync(self):
        from blueprints.komga import read_progress
        with self.app.app_context(), \
                mock.patch('blueprints.komga.config_store.load_komga_config', return_value={'enabled': True, 'url': 'http://komga', 'api_key': 'k'}), \
                mock.patch('blueprints.komga.client.load_komga_config', return_value={'enabled': True, 'url': 'http://komga', 'api_key': 'k'}, create=True), \
                mock.patch('blueprints.komga.client.KomgaClient.__init__', lambda self: setattr(self, 'base_url', 'http://komga') or setattr(self, 'headers', {})), \
                mock.patch('blueprints.komga.client.requests.get', side_effect=[_page(BOOKS[:2], False), _page(BOOKS[2:], True)]) as get:
            stats = read_progress.sync_read_progress()
        return stats, get

    def test_sync_reads_every_page_and_counts(self):
        stats, get = self.sync()
        self.assertEqual(stats, {'books': 4, 'read': 1, 'reading': 2, 'relinked': 1,
                                 'linked_volumes': 4, 'volumes_with_progress': 3})
        self.assertEqual(get.call_args_list[1].kwargs['params']['page'], 1)

    def test_unlinked_volume_is_linked_by_exact_filename(self):
        self.sync()
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute('SELECT komga_book_id FROM volumes WHERE series_id = 2').fetchone()[0], 'k9')
        conn.close()

    def test_keep_reading_lists_started_albums_latest_first(self):
        from blueprints.komga.read_progress import keep_reading
        self.sync()
        with self.app.app_context():
            items = keep_reading(1)
        self.assertEqual([(i['series_title'], i['label'], i['page']) for i in items],
                         [('Blake', 'Tome 1', 5), ('Astérix', 'Tome 2', 12)])

    def test_library_and_volumes_show_the_progress(self):
        from blueprints.library import routes
        self.sync()
        with self.app.test_request_context():
            series = {s['id']: s for s in routes.get_library_series(1).get_json()}
            volumes = {v['volume_number']: v for v in routes.get_series_volumes(1).get_json() if v.get('filepath')}
        self.assertEqual((series[1]['komga_book_count'], series[1]['komga_read_count'], series[1]['komga_reading_count']), (3, 1, 1))
        # Blake : son tome, jamais rattaché, l'a été par son nom de fichier pendant la synchronisation.
        self.assertEqual((series[2]['komga_book_count'], series[2]['komga_read_count'], series[2]['komga_reading_count']), (1, 0, 1))
        self.assertEqual(volumes[1]['komga_read_completed'], 1)
        self.assertEqual((volumes[2]['komga_read_page'], volumes[2]['komga_pages_count']), (12, 46))
        self.assertIsNone(volumes[3]['komga_read_page'])

    def test_reader_progress_is_recorded_at_once(self):
        from blueprints.komga.read_progress import record_local_progress
        self.sync()
        with self.app.app_context():
            record_local_progress('b3', 20, False)
            record_local_progress('b2', 46, True)
        conn = sqlite3.connect(self.db)
        rows = dict((r[0], r[1:]) for r in conn.execute('SELECT komga_book_id, page, pages_count, completed FROM komga_read_progress'))
        conn.close()
        self.assertEqual(rows['b3'], (20, 50, 0))
        self.assertEqual(rows['b2'], (46, 46, 1))

    def test_rescan_keeps_the_progress(self):
        # Le scan supprime puis recrée les lignes volumes en gardant komga_book_id :
        # l'avancement, stocké à part, reste associé au livre.
        self.sync()
        conn = sqlite3.connect(self.db)
        conn.execute('DELETE FROM volumes WHERE series_id = 1')
        conn.execute("INSERT INTO volumes (series_id, volume_number, filename, filepath, komga_book_id) VALUES (1, 1, 'T1.cbz', '/BD/Astérix/T1.cbz', 'b1')")
        conn.commit()
        conn.close()
        from blueprints.library import routes
        with self.app.test_request_context():
            series = {s['id']: s for s in routes.get_library_series(1).get_json()}
        self.assertEqual(series[1]['komga_read_count'], 1)

    def test_disabled_komga_does_nothing(self):
        from blueprints.komga import read_progress
        with self.app.app_context(), \
                mock.patch('blueprints.komga.config_store.load_komga_config', return_value={'enabled': False}):
            self.assertIsNone(read_progress.sync_read_progress())


if __name__ == '__main__':
    unittest.main()
