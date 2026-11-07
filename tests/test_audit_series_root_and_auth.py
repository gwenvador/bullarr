"""Correctifs de l'audit : série posée à la racine d'une bibliothèque, clé de session,
limite d'essais de connexion par mot de passe."""
import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from flask import Flask
from werkzeug.security import generate_password_hash

from path_safety import series_folder_conflict


class SeriesFolderConflictTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.library = os.path.realpath(self.tmp.name)

    def test_library_root_is_never_owned_by_a_series(self):
        self.assertEqual(series_folder_conflict(self.library, self.library, []),
                         'dossier racine de la bibliothèque')

    def test_shared_and_parent_folders_are_detected(self):
        own = os.path.join(self.library, 'Astérix')
        self.assertEqual(series_folder_conflict(own, self.library, [own]), 'dossier partagé avec une autre série')
        self.assertEqual(series_folder_conflict(own, self.library, [os.path.join(own, 'Hors-série')]),
                         "dossier qui contient celui d'une autre série")

    def test_own_folder_is_free(self):
        own = os.path.join(self.library, 'Astérix')
        others = [os.path.join(self.library, 'Astérix et Obélix'), os.path.join(self.library, 'Blake')]
        self.assertIsNone(series_folder_conflict(own, self.library, others))


def _make_db(path, library, rows):
    """rows: [(series_id, title, series_path, [filepaths])]"""
    conn = sqlite3.connect(path)
    conn.executescript('''
        CREATE TABLE libraries (id INTEGER PRIMARY KEY, path TEXT, name TEXT);
        CREATE TABLE series (id INTEGER PRIMARY KEY, library_id INTEGER, title TEXT, path TEXT);
        CREATE TABLE volumes (id INTEGER PRIMARY KEY AUTOINCREMENT, series_id INTEGER, filename TEXT, filepath TEXT);
    ''')
    conn.execute('INSERT INTO libraries VALUES (1, ?, ?)', (library, 'BD'))
    for series_id, title, series_path, files in rows:
        conn.execute('INSERT INTO series VALUES (?, 1, ?, ?)', (series_id, title, series_path))
        for filepath in files:
            conn.execute('INSERT INTO volumes (series_id, filename, filepath) VALUES (?, ?, ?)',
                         (series_id, os.path.basename(filepath), filepath))
    conn.commit()
    conn.close()


def _touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as handle:
        handle.write(b'x')
    return path


class RootSeriesTest(unittest.TestCase):
    """Un fichier posé à la racine crée une série dont le dossier EST la bibliothèque."""

    def setUp(self):
        from blueprints.library import routes
        self.routes = routes
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = os.path.realpath(self.tmp.name)
        self.library = os.path.join(base, 'BD')
        self.root_file = _touch(os.path.join(self.library, 'Astérix - T01.cbz'))
        self.other_file = _touch(os.path.join(self.library, 'Blake et Mortimer', 'T01.cbz'))
        self.db = os.path.join(base, 'bullarr.db')
        _make_db(self.db, self.library, [
            (1, 'Astérix', self.library, [self.root_file]),
            (2, 'Blake et Mortimer', os.path.join(self.library, 'Blake et Mortimer'), [self.other_file]),
        ])
        self.app = Flask(__name__)
        self.app.config['DATABASE'] = self.db

    def test_deleting_a_root_series_keeps_the_library(self):
        with self.app.test_request_context(), \
                mock.patch('blueprints.library.action_history.log_action'):
            response = self.routes.delete_series(1)
        self.assertTrue(response.get_json()['success'])
        self.assertTrue(os.path.isdir(self.library))
        self.assertTrue(os.path.isfile(self.other_file))
        self.assertFalse(os.path.exists(self.root_file))
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute('SELECT id FROM series ORDER BY id').fetchall(), [(2,)])
        conn.close()

    def test_regular_series_folder_is_still_removed(self):
        with self.app.test_request_context(), \
                mock.patch('blueprints.library.action_history.log_action'):
            response = self.routes.delete_series(2)
        self.assertTrue(response.get_json()['success'])
        self.assertFalse(os.path.exists(os.path.join(self.library, 'Blake et Mortimer')))
        self.assertTrue(os.path.isfile(self.root_file))

    def test_renaming_a_root_series_never_moves_the_library(self):
        with self.app.app_context():
            conn = self.routes.get_db_connection()
            try:
                result = self.routes._rename_series_folder(
                    conn, 1, self.library, 'Astérix', self.library, {}, custom_name='Astérix')
            finally:
                conn.close()
        self.assertFalse(result['success'])
        self.assertIn('racine', result['error'])
        self.assertTrue(os.path.isfile(self.root_file))
        self.assertTrue(os.path.isfile(self.other_file))


class SecretKeyTest(unittest.TestCase):
    def test_missing_or_public_key_is_replaced_by_a_persisted_random_key(self):
        from config import INSECURE_DEFAULT_SECRET_KEY, resolve_secret_key
        with tempfile.TemporaryDirectory() as data_dir:
            first = resolve_secret_key(None, data_dir)
            self.assertGreaterEqual(len(first), 40)
            self.assertNotEqual(first, INSECURE_DEFAULT_SECRET_KEY)
            self.assertEqual(resolve_secret_key('', data_dir), first)
            self.assertEqual(resolve_secret_key(INSECURE_DEFAULT_SECRET_KEY, data_dir), first)
            self.assertEqual(oct(os.stat(os.path.join(data_dir, '.secret_key')).st_mode & 0o777), '0o600')

    def test_explicit_key_is_kept(self):
        from config import resolve_secret_key
        with tempfile.TemporaryDirectory() as data_dir:
            self.assertEqual(resolve_secret_key('ma-cle', data_dir), 'ma-cle')
            self.assertFalse(os.path.exists(os.path.join(data_dir, '.secret_key')))


class PasswordLoginRateLimitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.makedirs('data', exist_ok=True)
        from app import create_app
        cls.app = create_app()
        cls.app.config['TESTING'] = True

    def setUp(self):
        from blueprints.auth import routes as auth_routes
        auth_routes._password_login_failures.clear()
        self.config_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.config_dir.cleanup)
        path = os.path.join(self.config_dir.name, 'oidc_config.json')
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump({'mode': 'password', 'username': 'admin',
                       'password_hash': generate_password_hash('secret-pass')}, handle)
        self.app.config['OIDC_CONFIG_FILE'] = path
        # Base à part : l'ancien compte unique y est migré en compte local.
        import sqlite3
        from blueprints.auth.roles import init_tables
        db_path = os.path.join(self.config_dir.name, 'bullarr.db')
        conn = sqlite3.connect(db_path)
        init_tables(conn.cursor())
        conn.commit()
        conn.close()
        previous_db = self.app.config.get('DATABASE')
        self.addCleanup(self.app.config.__setitem__, 'DATABASE', previous_db)
        self.app.config['DATABASE'] = db_path
        self.client = self.app.test_client()

    def login(self, password):
        return self.client.post('/login/password', data={'username': 'admin', 'password': password})

    def test_repeated_failures_are_refused_even_with_the_right_password(self):
        for _ in range(10):
            self.assertEqual(self.login('wrong').status_code, 401)
        self.assertEqual(self.login('wrong').status_code, 429)
        self.assertEqual(self.login('secret-pass').status_code, 429)

    def test_success_resets_the_counter(self):
        for _ in range(9):
            self.login('wrong')
        self.assertEqual(self.login('secret-pass').status_code, 302)
        self.assertEqual(self.login('wrong').status_code, 401)


if __name__ == '__main__':
    unittest.main()
