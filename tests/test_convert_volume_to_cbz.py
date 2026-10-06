import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from flask import Flask

from blueprints.bedetheque import routes


class ConvertVolumeToCbzTest(unittest.TestCase):
    """Régression: les routes convert-cbr/pdf/zip plantaient avec un NameError masqué par
    le `except Exception` (_conversion_lock, classify_archive et
    convert_mislabeled_archive_in_place n'étaient pas importés)."""

    def setUp(self):
        self.db = tempfile.NamedTemporaryFile(suffix='.db')
        conn = sqlite3.connect(self.db.name)
        conn.execute('CREATE TABLE series (id INTEGER PRIMARY KEY, title TEXT)')
        conn.execute('CREATE TABLE volumes (id INTEGER PRIMARY KEY, series_id INTEGER, filename TEXT, filepath TEXT, format TEXT, comicinfo TEXT)')
        conn.execute("INSERT INTO series VALUES (1, 'NOOB')")
        conn.execute("INSERT INTO volumes VALUES (1, 1, 'NOOB - #01.cbr', '/BD/NOOB/NOOB - #01.cbr', 'cbr', NULL)")
        conn.execute("INSERT INTO volumes VALUES (2, 1, 'NOOB - #02.cbz', '/BD/NOOB/NOOB - #02.cbz', 'cbr', NULL)")
        conn.commit()
        conn.close()
        self.app = Flask(__name__)
        self.app.config['DATABASE'] = self.db.name

    def tearDown(self):
        self.db.close()

    def _connect(self):
        conn = sqlite3.connect(self.db.name)
        conn.row_factory = sqlite3.Row
        return conn

    def _run(self, volume_id, convert_fn):
        patches = [
            mock.patch.object(routes, 'get_db_connection', self._connect),
            mock.patch.object(routes, '_update_volume_filepath_format'),
            mock.patch('blueprints.library.scanner.LibraryScanner'),
            mock.patch('blueprints.komga.client.trigger_scan_async'),
            mock.patch('blueprints.library.action_history.log_action'),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        with self.app.test_request_context():
            response = routes._convert_volume_to_cbz(volume_id, 'cbr', convert_fn, RuntimeError)
        return response

    def test_regular_conversion_reaches_the_converter_and_succeeds(self):
        convert_fn = mock.Mock(return_value='/BD/NOOB/NOOB - #01.cbz')
        response = self._run(1, convert_fn)
        convert_fn.assert_called_once_with('/BD/NOOB/NOOB - #01.cbr')
        self.assertEqual(response.get_json()['success'], True)
        self.assertEqual(response.get_json()['filename'], 'NOOB - #01.cbz')

    def test_mislabeled_rar_named_cbz_is_converted_in_place(self):
        convert_fn = mock.Mock()
        with mock.patch('blueprints.library.archive_converter.classify_archive', return_value='rar'), \
                mock.patch('blueprints.library.archive_converter.convert_mislabeled_archive_in_place',
                           return_value='/BD/NOOB/NOOB - #02.cbz') as in_place:
            response = self._run(2, convert_fn)
        in_place.assert_called_once_with('/BD/NOOB/NOOB - #02.cbz')
        convert_fn.assert_not_called()
        self.assertEqual(response.get_json()['success'], True)


if __name__ == '__main__':
    unittest.main()
