import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from blueprints.library import routes as library_routes
from blueprints.library.routes import library_bp

ROOT = Path(__file__).resolve().parents[1]


class _Shared:
    """Connexion partagée du test : close() ne ferme rien."""

    def __init__(self, conn):
        self._conn = conn

    def cursor(self):
        return self._conn.cursor()

    def execute(self, *args):
        return self._conn.execute(*args)

    def close(self):
        pass


class SeriesVolumesOwnershipTest(unittest.TestCase):
    """GET /api/series/<id>/volumes : is_owned pour marquer les albums déjà en bibliothèque (import, déplacement)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        conn = sqlite3.connect(os.path.join(self.tmp.name, 'db.sqlite'))
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE series (id INTEGER PRIMARY KEY, title TEXT, bedetheque_albums TEXT);
            CREATE TABLE volumes (id INTEGER PRIMARY KEY, series_id INTEGER, part_number INTEGER, volume_number INTEGER,
                integral_number INTEGER, hs_number INTEGER, is_integral INTEGER DEFAULT 0, is_hs INTEGER DEFAULT 0,
                is_episode INTEGER DEFAULT 0, episode_number INTEGER, filepath TEXT, filesystem_present INTEGER,
                comicinfo TEXT);
            INSERT INTO series VALUES (1, 'Sans catalogue', NULL), (2, 'Avec catalogue', NULL);
            INSERT INTO volumes (id, series_id, volume_number, filepath, filesystem_present) VALUES
                (10, 1, 1, '/lib/s/1.cbz', 1), (11, 1, 2, NULL, NULL), (12, 1, 3, '/lib/s/3.cbz', 0),
                (13, 1, 4, '/lib/s/4.cbz', NULL), (14, 1, 5, '   ', 1);
            INSERT INTO volumes (id, series_id, volume_number, filepath, filesystem_present) VALUES (20, 2, 1, '/lib/a/1.cbz', 1);
        """)
        albums = [{'number': n, 'title': f'Album {n}', 'url': f'https://example.test/{n}'} for n in (1, 2, 3)]
        conn.execute('UPDATE series SET bedetheque_albums = ? WHERE id = 2', (json.dumps(albums),))
        conn.commit()
        app = Flask(__name__)
        app.register_blueprint(library_bp)
        self.client = app.test_client()
        patcher = patch.object(library_routes, 'get_db_connection', side_effect=lambda: _Shared(conn))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_volume_with_a_file_is_owned_placeholder_missing_or_blank_path_is_not(self):
        volumes = self.client.get('/api/series/1/volumes').get_json()
        owned = {volume['volume_number']: volume['is_owned'] for volume in volumes}
        self.assertEqual(owned, {1: True, 2: False, 3: False, 4: True, 5: False})

    def test_catalogue_albums_without_file_are_listed_as_not_owned(self):
        volumes = self.client.get('/api/series/2/volumes').get_json()
        owned = {volume['volume_number']: volume['is_owned'] for volume in volumes}
        self.assertEqual(owned, {1: True, 2: False, 3: False})

    def test_import_dropdown_and_move_dialog_read_the_flag(self):
        nav = (ROOT / 'static/js/nav.js').read_text()
        self.assertIn('if (showOwned && v.is_owned) label = `✓ ${label}`;', nav)
        self.assertIn('showOwned: true', (ROOT / 'static/js/import.js').read_text())


if __name__ == '__main__':
    unittest.main()
