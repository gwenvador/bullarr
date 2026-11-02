import os
import tempfile
import unittest
from pathlib import Path

from flask import Flask

from blueprints.library import routes
from blueprints.library.routes import UPLOAD_STAGING_DIRNAME, _series_upload_dir, cleanup_empty_directories

ROOT = Path(__file__).resolve().parents[1]


class SeriesUploadDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.library = os.path.join(self.tmp.name, 'BD')
        os.makedirs(self.library)

    def row(self, path, title='Sillage'):
        return {'library_path': self.library, 'path': path, 'title': title}

    def test_file_is_received_inside_the_series_folder(self):
        series = os.path.join(self.library, 'Sillage')
        os.makedirs(series)
        upload_dir = _series_upload_dir(self.row(series))
        self.assertEqual(upload_dir, os.path.join(series, UPLOAD_STAGING_DIRNAME))
        self.assertTrue(os.path.isdir(upload_dir))

    def test_series_without_a_recorded_path_uses_its_title_in_the_library(self):
        upload_dir = _series_upload_dir(self.row(None, 'Nouvelle série'))
        self.assertEqual(upload_dir, os.path.join(self.library, 'Nouvelle série', UPLOAD_STAGING_DIRNAME))

    def test_path_outside_the_library_is_refused(self):
        self.assertIsNone(_series_upload_dir(self.row(os.path.join(self.tmp.name, 'ailleurs'))))

    def test_staging_folder_is_an_accepted_import_root_only_while_it_exists(self):
        series = os.path.join(self.library, 'Sillage')
        os.makedirs(series)
        import sqlite3
        db = os.path.join(self.tmp.name, 'bullarr.db')
        conn = sqlite3.connect(db)
        conn.execute('CREATE TABLE series (id INTEGER PRIMARY KEY, path TEXT)')
        conn.execute('INSERT INTO series (path) VALUES (?)', (series,))
        conn.commit()
        conn.close()
        app = Flask(__name__)
        app.config['DATABASE'] = db
        with app.app_context():
            self.assertEqual(routes._series_upload_roots(), [])
            upload_dir = _series_upload_dir(self.row(series))
            self.assertEqual(routes._series_upload_roots(), [upload_dir])

    def test_empty_staging_folder_is_removed_after_the_import(self):
        series = os.path.join(self.library, 'Sillage')
        upload_dir = _series_upload_dir(self.row(series))
        cleanup_empty_directories(upload_dir)
        self.assertFalse(os.path.exists(upload_dir))
        self.assertTrue(os.path.isdir(series))


class HiddenFoldersAreNotSeriesTest(unittest.TestCase):
    def test_scanner_ignores_hidden_folders_when_looking_for_universe_subfolders(self):
        script = (ROOT / 'blueprints/library/scanner.py').read_text()
        self.assertIn("for name in child_names if not name.startswith('.'))", script)
        self.assertIn("if sub_name.startswith('.') or not os.path.isdir(sub_path):", script)


class UploadProgressTest(unittest.TestCase):
    def test_upload_shows_a_percentage_and_reports_non_json_errors(self):
        script = (ROOT / 'static/js/library.js').read_text()
        self.assertIn('request.upload.onprogress', script)
        self.assertIn('Envoi de « ${file.name} » : ${progress.percent} %', script)
        self.assertIn('traitement par le serveur', script)
        start = script.index('async function uploadSeriesFile(')
        body = script[start:script.index('async function toggleOneshot(', start)]
        self.assertNotIn("fetch(`/api/series/${seriesId}/upload-file`", body)


    def test_upload_sends_the_csrf_token_like_every_other_write(self):
        # Régression: l'envoi en XMLHttpRequest contournait le fetch de nav.js (jeton ajouté aux écritures) et
        # le serveur répondait « Jeton de sécurité manquant ou invalide ».
        script = (ROOT / 'static/js/library.js').read_text()
        start = script.index('async function _uploadWithProgress(')
        body = script[start:script.index('async function uploadSeriesFile(', start)]
        self.assertIn('await window.getCsrfToken()', body)
        self.assertIn("setRequestHeader('X-CSRF-Token', csrfToken)", body)
        nav = (ROOT / 'static/js/nav.js').read_text()
        self.assertIn('window.getCsrfToken = getToken', nav)
        # Aucun autre XMLHttpRequest d'écriture oublié dans l'application.
        for path in (ROOT / 'static/js').glob('*.js'):
            if path.name == 'library.js':
                continue
            self.assertNotIn('new XMLHttpRequest', path.read_text(), path.name)


if __name__ == '__main__':
    unittest.main()
