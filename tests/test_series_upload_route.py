import io
import os
import sqlite3
import tempfile
import unittest

from flask import Flask

from config import Config


class UploadRouteIntoSeriesFolderTest(unittest.TestCase):
    """POST /api/series/<id>/upload-file reçoit le fichier dans le dossier de la série (pas dans un dossier d'import)."""

    def test_uploaded_file_lands_in_the_series_folder_and_its_root_is_accepted(self):
        from blueprints.library import library_bp
        from blueprints.library.routes import UPLOAD_STAGING_DIRNAME, _allowed_import_roots
        with tempfile.TemporaryDirectory() as tmp:
            library = os.path.join(tmp, 'BD')
            series = os.path.join(library, 'Sillage')
            os.makedirs(series)
            db = os.path.join(tmp, 'bullarr.db')
            conn = sqlite3.connect(db)
            conn.executescript("""
                CREATE TABLE libraries (id INTEGER PRIMARY KEY, name TEXT, path TEXT);
                CREATE TABLE series (id INTEGER PRIMARY KEY, title TEXT, path TEXT, library_id INTEGER);
            """)
            conn.execute("INSERT INTO libraries VALUES (1, 'BD', ?)", (library,))
            conn.execute("INSERT INTO series VALUES (7, 'Sillage', ?, 1)", (series,))
            conn.commit()
            conn.close()
            app = Flask(__name__)
            app.config.from_object(Config)
            app.config['DATABASE'] = db
            app.config['IMPORT_DIRECTORIES'] = [os.path.join(tmp, 'lecture-seule')]  # inexistant: ne doit servir à rien
            app.config['LIBRARY_IMPORT_CONFIG_FILE'] = os.path.join(tmp, 'import.json')
            app.register_blueprint(library_bp)
            response = app.test_client().post('/api/series/7/upload-file', data={
                'file': (io.BytesIO(b'PK\x03\x04 test'), 'Sillage - T14.cbz')})
            payload = response.get_json()
            self.assertEqual(response.status_code, 200, payload)
            staged = payload['file']['filepath']
            self.assertEqual(os.path.dirname(staged), os.path.join(series, UPLOAD_STAGING_DIRNAME))
            self.assertEqual(payload['file']['import_root'], os.path.join(series, UPLOAD_STAGING_DIRNAME))
            self.assertEqual(payload['file']['relative_path'], 'Sillage_-_T14.cbz')
            self.assertTrue(os.path.exists(staged))
            with app.app_context():
                self.assertIn(os.path.join(series, UPLOAD_STAGING_DIRNAME), _allowed_import_roots())
