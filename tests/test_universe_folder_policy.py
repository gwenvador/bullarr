import json
import os
import shutil
import sqlite3
import tempfile
import unittest

from config import Config


class UniverseFolderPolicyTest(unittest.TestCase):
    def setUp(self):
        os.makedirs('data', exist_ok=True)
        fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        Config._init_database(self.db_path)
        self.tmp_root = tempfile.mkdtemp()
        self.library_path = os.path.join(self.tmp_root, 'BD')
        self.series_path = os.path.join(self.library_path, 'Kriss de Valnor')
        os.makedirs(self.series_path)
        with open(os.path.join(self.series_path, 'tome1.cbz'), 'wb') as handle:
            handle.write(b'fake-cbz')

        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO libraries (id, name, path) VALUES (1, 'BD', ?)", (self.library_path,))
        conn.execute("INSERT INTO universes (id, name) VALUES (1, 'Thorgal')")
        conn.execute("""INSERT INTO series (id, library_id, title, path, total_volumes, missing_volumes, has_parts, bedetheque_url)
                        VALUES (1206, 1, 'Kriss de Valnor', ?, 1, '[]', 0, ?)""",
                     (self.series_path, 'https://example.invalid/kriss'))
        conn.execute("INSERT INTO universe_series (universe_id, bedetheque_url, title, series_id) VALUES (1, ?, 'Thorgal', NULL)",
                     ('https://example.invalid/thorgal',))
        conn.commit()
        conn.close()

        from app import create_app
        self.app = create_app()
        self.rename_config_path = os.path.join(self.tmp_root, 'rename_config.json')
        with open(self.rename_config_path, 'w') as handle:
            json.dump({'series_template': '{<univers>/}<series>'}, handle)
        self.app.config.update(DATABASE=self.db_path, RENAME_CONFIG_FILE=self.rename_config_path, TESTING=True)
        self.client = self.app.test_client()

    def tearDown(self):
        os.remove(self.db_path)
        shutil.rmtree(self.tmp_root, ignore_errors=True)

    def expected_path(self):
        return os.path.join(self.library_path, 'Thorgal', 'Kriss de Valnor')

    def test_manual_universe_assignment_moves_existing_series_folder(self):
        response = self.client.put('/api/series/1206/manual-metadata', json={'universe_id': 1})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertTrue(response.get_json()['success'])
        conn = sqlite3.connect(self.db_path)
        path = conn.execute('SELECT path FROM series WHERE id=1206').fetchone()[0]
        conn.close()
        self.assertEqual(path, self.expected_path())
        self.assertTrue(os.path.exists(os.path.join(path, 'tome1.cbz')))

    def test_bedetheque_universe_removal_moves_existing_series_folder_back_to_root(self):
        nested = self.expected_path()
        os.makedirs(os.path.dirname(nested), exist_ok=True)
        os.rename(self.series_path, nested)
        conn = sqlite3.connect(self.db_path)
        conn.execute('UPDATE series SET path=?, universe_id=1 WHERE id=1206', (nested,))
        conn.commit()
        conn.close()
        from blueprints.bedetheque.scraper import BedethequeDatabase
        with self.app.app_context():
            BedethequeDatabase(self.db_path).sync_series_universe(1206, {'url': 'https://example.invalid/kriss', 'title': 'Kriss de Valnor', 'related_series': []})
        conn = sqlite3.connect(self.db_path)
        path, universe_id = conn.execute('SELECT path, universe_id FROM series WHERE id=1206').fetchone()
        conn.close()
        self.assertIsNone(universe_id)
        self.assertEqual(path, self.series_path)
        self.assertTrue(os.path.exists(os.path.join(path, 'tome1.cbz')))

    def test_bedetheque_universe_sync_moves_existing_series_folder(self):
        from blueprints.bedetheque.scraper import BedethequeDatabase
        info = {
            'url': 'https://example.invalid/kriss',
            'title': 'Kriss de Valnor',
            'related_series': [{'url': 'https://example.invalid/thorgal', 'title': 'Thorgal'}],
        }
        with self.app.app_context():
            BedethequeDatabase(self.db_path).sync_series_universe(1206, info)
        conn = sqlite3.connect(self.db_path)
        path, universe_id = conn.execute('SELECT path, universe_id FROM series WHERE id=1206').fetchone()
        conn.close()
        self.assertEqual(universe_id, 1)
        self.assertEqual(path, self.expected_path())
        self.assertTrue(os.path.exists(os.path.join(path, 'tome1.cbz')))


if __name__ == '__main__':
    unittest.main()
