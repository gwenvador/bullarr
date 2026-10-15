import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask

from blueprints.library import library_bp, routes
from blueprints.library import scanner as scanner_module
from blueprints.library.scanner import LibraryScanner


class FormatTagIsNotReleaserTest(unittest.TestCase):
    def test_format_in_brackets_is_not_a_releaser(self):
        for name in ('Chagrin.Rodolphe.Griffo.2026.FR.[PDF]-NOTAG.pdf',
                     'Hulkverines.Greg.Pak.2019.FR.[CBR].[CBZ]-NOTAG.cbz',
                     'Blacksad - T01 - [CBZ].cbz'):
            parsed = LibraryScanner.parse_filename(name)
            self.assertIsNone(parsed['group'], name)
            self.assertIsNone(parsed['author'], name)

    def test_real_releaser_is_kept(self):
        self.assertEqual(LibraryScanner.parse_filename('Blacksad - T01 [Team].cbz')['group'], 'Team')
        self.assertEqual(LibraryScanner.parse_filename('Les Pompiers - T01 [PDF] (Collectif).pdf')['author'], 'Collectif')


class StoredFormatReleaserCleanupTest(unittest.TestCase):
    def test_format_releaser_already_stored_is_cleared(self):
        fd, db_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        try:
            LibraryScanner(db_path)
            conn = sqlite3.connect(db_path)
            conn.execute("INSERT INTO volumes (id, series_id, filename, release_group) VALUES (1, 1, 'a', 'PDF'), (2, 1, 'b', 'Team')")
            conn.commit()
            conn.close()
            scanner_module._FORMAT_RELEASER_CLEANED.discard(db_path)
            LibraryScanner(db_path)
            conn = sqlite3.connect(db_path)
            groups = dict(conn.execute('SELECT id, release_group FROM volumes').fetchall())
            conn.close()
            self.assertEqual(groups, {1: None, 2: 'Team'})
        finally:
            os.remove(db_path)


class RenameOnQualityChangeTest(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        conn = sqlite3.connect(self.db_path)
        conn.execute('CREATE TABLE volumes (id INTEGER PRIMARY KEY, series_id INTEGER, filepath TEXT, filename TEXT, '
                     'format TEXT, resolution TEXT, release_group TEXT)')
        conn.execute("INSERT INTO volumes VALUES (1, 7, '/lib/a.cbz', 'a.cbz', 'cbz', NULL, 'PDF')")
        conn.commit()
        conn.close()
        app = Flask(__name__)
        app.config.update(DATABASE=self.db_path, TESTING=True)
        app.register_blueprint(library_bp)
        self.client = app.test_client()

    def tearDown(self):
        os.remove(self.db_path)

    def _put(self, body):
        with patch.object(routes, '_rename_single_volume') as rename, \
             patch.object(routes.LibraryScanner, 'update_series_stats'), \
             patch('blueprints.komga.client.trigger_scan_async'):
            response = self.client.put('/api/volumes/1/manual-metadata', json=body)
        return response, rename

    def test_changed_releaser_renames_file(self):
        response, rename = self._put({'resolution': '', 'release_group': 'NOTAG'})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        rename.assert_called_once_with(7, 1, 'édition qualité/releaser')

    def test_unchanged_values_do_not_rename(self):
        response, rename = self._put({'resolution': '', 'release_group': 'PDF'})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        rename.assert_not_called()


if __name__ == '__main__':
    unittest.main()
