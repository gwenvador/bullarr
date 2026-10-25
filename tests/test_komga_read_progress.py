import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from flask import Flask

from blueprints.komga import client as komga_client
from blueprints.library import routes as library_routes
from blueprints.library.routes import library_bp

ROOT = Path(__file__).resolve().parents[1]


class KomgaReadProgressClientTest(unittest.TestCase):
    def _client(self):
        with patch.object(komga_client, 'load_komga_config', return_value={
                'enabled': True, 'url': 'https://komga.example', 'api_key_decrypted': 'k'}):
            return komga_client.KomgaClient()

    def test_in_progress_sends_the_page_and_completed_sends_only_the_flag(self):
        client = self._client()
        ok = MagicMock(status_code=204)
        with patch.object(komga_client.requests, 'patch', return_value=ok) as patched:
            client.update_read_progress('BOOK1', 12)
            client.update_read_progress('BOOK1', 40, completed=True)
        first, second = patched.call_args_list
        self.assertEqual(first.args[0], 'https://komga.example/api/v1/books/BOOK1/read-progress')
        self.assertEqual(first.kwargs['json'], {'page': 12})
        self.assertEqual(second.kwargs['json'], {'completed': True})
        self.assertEqual(first.kwargs['headers']['X-API-Key'], 'k')

    def test_komga_error_is_reported(self):
        client = self._client()
        with patch.object(komga_client.requests, 'patch', return_value=MagicMock(status_code=400, text='bad page')):
            with self.assertRaises(komga_client.KomgaError):
                client.update_read_progress('BOOK1', 999)


class ReaderProgressRouteTest(unittest.TestCase):
    """POST /api/volumes/<id>/reader/progress : envoi à Komga, jamais bloquant."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.album = os.path.join(self.tmp.name, 'album.cbz')
        with open(self.album, 'wb') as handle:
            handle.write(b'x')
        conn = sqlite3.connect(os.path.join(self.tmp.name, 'db.sqlite'))
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE series (id INTEGER PRIMARY KEY, title TEXT);
            CREATE TABLE volumes (id INTEGER PRIMARY KEY, series_id INTEGER, filepath TEXT, format TEXT,
                volume_number INTEGER, is_integral INTEGER, integral_number INTEGER, is_hs INTEGER, hs_number INTEGER,
                filename TEXT, comicinfo TEXT, komga_book_id TEXT);
            INSERT INTO series VALUES (1, 'Serie');
        """)
        conn.execute("INSERT INTO volumes (id, series_id, filepath, format, komga_book_id) VALUES (1, 1, ?, 'cbz', 'BOOK1')", (self.album,))
        conn.execute("INSERT INTO volumes (id, series_id, filepath, format) VALUES (2, 1, ?, 'cbz')", (self.album,))
        conn.commit()
        self.conn = conn
        app = Flask(__name__)
        app.register_blueprint(library_bp)
        self.client = app.test_client()
        patcher = patch.object(library_routes, 'get_db_connection', side_effect=lambda: _Shared(self.conn))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _post(self, volume_id, payload, config):
        with patch('blueprints.komga.config_store.load_komga_config', return_value=config), \
             patch('blueprints.komga.client.KomgaClient') as client_class:
            response = self.client.post(f'/api/volumes/{volume_id}/reader/progress', json=payload)
            return response, client_class

    def test_sends_page_and_completion_to_the_komga_book(self):
        response, client_class = self._post(1, {'page': 7}, {'enabled': True})
        self.assertEqual(response.get_json(), {'success': True, 'synced': True})
        client_class.return_value.update_read_progress.assert_called_once_with('BOOK1', 7, False)
        _, client_class = self._post(1, {'page': 30, 'completed': True}, {'enabled': True})
        client_class.return_value.update_read_progress.assert_called_once_with('BOOK1', 30, True)

    def test_nothing_is_sent_when_disabled_switched_off_or_not_in_komga(self):
        for config, volume, reason in (({'enabled': False}, 1, 'disabled'),
                                       ({'enabled': True, 'sync_read_progress': False}, 1, 'disabled'),
                                       ({'enabled': True}, 2, 'no_komga_book')):
            response, client_class = self._post(volume, {'page': 3}, config)
            self.assertEqual(response.get_json()['reason'], reason)
            self.assertFalse(response.get_json()['synced'])
            client_class.return_value.update_read_progress.assert_not_called()

    def test_invalid_page_is_refused_and_komga_failure_never_breaks_reading(self):
        response, _ = self._post(1, {'page': 'x'}, {'enabled': True})
        self.assertEqual(response.status_code, 400)
        from blueprints.komga.client import KomgaError
        with patch('blueprints.komga.config_store.load_komga_config', return_value={'enabled': True}), \
             patch('blueprints.komga.client.KomgaClient') as client_class:
            client_class.return_value.update_read_progress.side_effect = KomgaError('HTTP 400')
            response = self.client.post('/api/volumes/1/reader/progress', json={'page': 5})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['reason'], 'komga_error')


class _Shared:
    """Connexion partagée du test : close() ne ferme rien."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, *args):
        return self._conn.execute(*args)

    def close(self):
        pass


class ReaderProgressFrontendTest(unittest.TestCase):
    def setUp(self):
        self.js = (ROOT / 'static/js/reader.js').read_text()

    def test_progress_is_debounced_flushed_on_close_and_not_sent_on_opening(self):
        self.assertIn('/reader/progress', self.js)
        self.assertIn('COMIC_READER_PROGRESS_DELAY_MS', self.js)
        show = self.js[self.js.index('function showComicReaderPage('):self.js.index('function stepComicReader(')]
        self.assertIn('if (comicReader.progressReady) _scheduleComicReaderProgress();', show)
        close = self.js[self.js.index('function closeComicReader('):]
        self.assertIn('_flushComicReaderProgress();', close)
        opening = self.js[self.js.index('async function openComicReader('):self.js.index('function closeComicReader(')]
        self.assertIn('comicReader.progressReady = false;', opening)

    def test_settings_expose_the_switch(self):
        self.assertIn('komgaSyncReadProgress', (ROOT / 'templates/settings.html').read_text())
        settings = (ROOT / 'static/js/settings.js').read_text()
        self.assertIn('sync_read_progress: document.getElementById', settings)


if __name__ == '__main__':
    unittest.main()
