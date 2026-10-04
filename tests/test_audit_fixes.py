"""Regression checks for backup, extraction, request and network safety fixes."""

import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from backup_utils import apply_pending_restore
from blueprints.auth import auth_bp
from blueprints.library import library_bp
from blueprints.library.routes import _extract_one_archive
from blueprints.settings import settings_bp
from network_safety import safe_external_get


class BackupSafetyTests(unittest.TestCase):
    def test_download_includes_committed_rows_still_in_wal(self):
        with tempfile.TemporaryDirectory() as directory:
            database = os.path.join(directory, 'bullarr.db')
            live = sqlite3.connect(database)
            live.execute('PRAGMA journal_mode=WAL')
            live.execute('CREATE TABLE entries (value TEXT)')
            live.commit()
            live.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            live.execute('INSERT INTO entries VALUES (?)', ('latest',))
            live.commit()

            app = Flask(__name__)
            app.config.update(TESTING=True, DATA_DIR=directory)
            app.register_blueprint(settings_bp)
            with patch('blueprints.settings.routes._backup_file_specs', return_value=[('bullarr.db', database, True)]):
                with app.test_client() as client:
                    response = client.get('/api/settings/backup')
                    self.assertEqual(response.status_code, 200)
                    archive = zipfile.ZipFile(io.BytesIO(response.data))
                    snapshot = os.path.join(directory, 'snapshot.db')
                    Path(snapshot).write_bytes(archive.read('bullarr.db'))
                    with closing(sqlite3.connect(snapshot)) as restored:
                        self.assertEqual(restored.execute('SELECT value FROM entries').fetchone()[0], 'latest')
                    response.close()
            live.close()

    def test_restore_is_staged_until_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            live_db = Path(directory, 'bullarr.db')
            replacement_db = Path(directory, 'replacement.db')
            for database, value in ((live_db, 'live'), (replacement_db, 'restored')):
                with closing(sqlite3.connect(database)) as conn:
                    conn.execute('CREATE TABLE entries (value TEXT)')
                    conn.execute('INSERT INTO entries VALUES (?)', (value,))
                    conn.commit()
            archive_bytes = io.BytesIO()
            with zipfile.ZipFile(archive_bytes, 'w') as archive:
                archive.write(replacement_db, 'bullarr.db')
            archive_bytes.seek(0)

            app = Flask(__name__)
            app.config.update(TESTING=True, DATA_DIR=directory)
            app.register_blueprint(settings_bp)
            with patch('blueprints.settings.routes._backup_file_specs', return_value=[('bullarr.db', str(live_db), True)]):
                with app.test_client() as client:
                    response = client.post('/api/settings/backup/restore', data={
                        'file': (archive_bytes, 'backup.zip'),
                    })
                    self.assertEqual(response.status_code, 200)
            with closing(sqlite3.connect(live_db)) as conn:
                self.assertEqual(conn.execute('SELECT value FROM entries').fetchone()[0], 'live')
            self.assertTrue(Path(directory, '.pending_restore').is_dir())
            apply_pending_restore(directory)
            with closing(sqlite3.connect(live_db)) as conn:
                self.assertEqual(conn.execute('SELECT value FROM entries').fetchone()[0], 'restored')

    def test_pending_restore_replaces_database_before_startup_and_removes_old_wal(self):
        with tempfile.TemporaryDirectory() as directory:
            old_db = Path(directory, 'bullarr.db')
            subprocess.run([sys.executable, '-c', '''
import os, sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute('PRAGMA journal_mode=WAL')
conn.execute('CREATE TABLE entries (value TEXT)')
conn.commit()
conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
conn.execute('INSERT INTO entries VALUES (?)', ('old',))
conn.commit()
os._exit(0)
''', str(old_db)], check=True)
            pending = Path(directory, '.pending_restore')
            pending.mkdir()
            with closing(sqlite3.connect(pending / 'bullarr.db')) as conn:
                conn.execute('CREATE TABLE entries (value TEXT)')
                conn.execute('INSERT INTO entries VALUES (?)', ('new',))
                conn.commit()
            (pending / 'manifest.json').write_text(json.dumps({'files': ['bullarr.db']}))

            self.assertEqual(apply_pending_restore(directory), ['bullarr.db'])
            with closing(sqlite3.connect(old_db)) as conn:
                self.assertEqual(conn.execute('SELECT value FROM entries').fetchone()[0], 'new')
            self.assertFalse(Path(str(old_db) + '-wal').exists())
            with closing(sqlite3.connect(str(old_db) + '.before_restore')) as conn:
                self.assertEqual(conn.execute('SELECT value FROM entries').fetchone()[0], 'old')


class ExtractionSafetyTests(unittest.TestCase):
    def test_existing_file_and_source_archive_are_preserved_on_collision(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory, 'album')
            target.mkdir()
            (target / 'page.jpg').write_bytes(b'original')
            archive = Path(directory, 'album.zip')
            with zipfile.ZipFile(archive, 'w') as zf:
                zf.writestr('nested/page.jpg', b'replacement')
            with self.assertRaises(FileExistsError):
                _extract_one_archive(str(archive), '.zip', str(target))
            self.assertEqual((target / 'page.jpg').read_bytes(), b'original')
            self.assertTrue(archive.exists())

    def test_flattened_duplicate_names_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory, 'album.zip')
            with zipfile.ZipFile(archive, 'w') as zf:
                zf.writestr('one/page.jpg', b'one')
                zf.writestr('two/page.jpg', b'two')
            with self.assertRaises(ValueError):
                _extract_one_archive(str(archive), '.zip', str(Path(directory, 'album')))
            self.assertTrue(archive.exists())


class RequestSafetyTests(unittest.TestCase):
    def test_scan_requires_post_and_oversize_series_upload_is_rejected_early(self):
        app = Flask(__name__)
        app.config.update(TESTING=True, MAX_CONTENT_LENGTH=4 * 1024 * 1024 * 1024)
        app.register_blueprint(library_bp)
        with app.test_client() as client:
            self.assertEqual(client.get('/api/scan/1').status_code, 405)
            response = client.post('/api/series/1/upload-file', environ_overrides={
                'CONTENT_LENGTH': str(2 * 1024 * 1024 * 1024 + 2 * 1024 * 1024),
            })
            self.assertEqual(response.status_code, 413)

    def test_authenticated_write_requires_session_csrf_token(self):
        app = Flask(__name__)
        app.config.update(TESTING=True, SECRET_KEY='test-only')
        app.register_blueprint(auth_bp)
        app.add_url_rule('/protected', 'protected', lambda: 'ok', methods=['POST'])
        with patch('blueprints.auth.routes.load_oidc_config', return_value={'mode': 'password'}):
            with app.test_client() as client:
                with client.session_transaction() as session:
                    session['user'] = {'name': 'reader'}
                self.assertEqual(client.post('/protected').status_code, 403)
                token = client.get('/api/auth/csrf').json['token']
                self.assertEqual(client.post('/protected', headers={'X-CSRF-Token': token}).status_code, 200)

    def test_external_get_rejects_private_redirect_before_following_it(self):
        import requests
        redirect = requests.Response()
        redirect.status_code = 302
        redirect.headers['Location'] = 'http://127.0.0.1/internal'
        redirect.raw = io.BytesIO()
        fake_session = unittest.mock.MagicMock()
        fake_session.get.return_value = redirect
        def resolve(host, port, **kwargs):
            address = '127.0.0.1' if host == '127.0.0.1' else '8.8.8.8'
            return [(2, 1, 6, '', (address, port))]
        with patch('network_safety.socket.getaddrinfo', side_effect=resolve):
            with patch('network_safety.requests.Session', return_value=fake_session):
                with self.assertRaises(ValueError):
                    safe_external_get('https://example.test/feed')
        self.assertEqual(fake_session.get.call_count, 1)


if __name__ == '__main__':
    unittest.main()
