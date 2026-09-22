import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from blueprints.library import routes
from blueprints.library import import_history
from blueprints.missing_monitor import downloader
from blueprints.library.routes import _merge_cached_bedetheque_comicinfo


class ImportMetadataCompletionTest(unittest.TestCase):
    def test_placeholder_completion_restores_full_cached_album_metadata(self):
        conn = sqlite3.connect(':memory:')
        conn.execute("CREATE TABLE series (id INTEGER PRIMARY KEY, bedetheque_url TEXT, bedetheque_albums TEXT, bedetheque_description TEXT, bedetheque_genre TEXT, bedetheque_scenaristes TEXT, bedetheque_dessinateurs TEXT, bedetheque_editeurs TEXT)")
        conn.execute("INSERT INTO series VALUES (1, ?, ?, ?, ?, ?, ?, ?)", (
            'https://example.invalid/series',
            json.dumps([{
                'number': 4, 'title': 'Escarmouches', 'scenario': 'Patrick Cothias',
                'dessin': 'David Prudhomme', 'couleurs': 'David Prudhomme',
                'editeur': 'Glénat', 'url': 'https://example.invalid/tome-4',
                'date_publication': '1997-09-01', 'rating': 4.8, 'rating_count': 4,
            }]),
            None, 'Histoire', 'Patrick Cothias', 'David Prudhomme', 'Glénat'))
        conn.commit()
        result = _merge_cached_bedetheque_comicinfo(conn.cursor(), 1, 'Ninon secrète', 4, {'title': 'Escarmouches'})
        self.assertEqual(result['series'], 'Ninon secrète')
        self.assertEqual(result['number'], '4')
        self.assertEqual(result['colorist'], 'David Prudhomme')
        self.assertEqual(result['month'], '9')
        self.assertEqual(result['communityrating'], '4.8')
        conn.close()

    def test_immediate_auto_import_path_can_compute_relative_parts(self):
        app = Flask(__name__)
        app.config['DATABASE'] = ':memory:'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            filepath = root / 'Mi-Mouche - T03.cbz'
            filepath.write_bytes(b'placeholder')
            with app.app_context(), patch.object(routes, 'load_library_import_config', return_value={
                'auto_import_enabled': True,
                'monitored_extensions': ['.cbz'],
            }), patch.object(import_history, 'get_manual_override_filepaths', return_value=set()), \
                 patch.object(import_history, 'get_in_progress_filepaths', return_value=set()), \
                 patch.object(downloader, 'get_trackable_active_downloads', return_value=[]), \
                 patch.object(routes, 'find_active_download_destination', return_value=None):
                output = StringIO()
                with redirect_stdout(output):
                    routes.attempt_immediate_auto_import(str(filepath), str(root))
                self.assertNotIn('Erreur import automatique immédiat', output.getvalue())


if __name__ == '__main__':
    unittest.main()
