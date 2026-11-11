import json
import os
import sqlite3
import tempfile
import unittest

from werkzeug.security import generate_password_hash

from config import Config


class UserApiKeysTest(unittest.TestCase):
    """Clés API personnelles : droits du compte, gestion depuis le profil, jeton par login."""

    def setUp(self):
        os.makedirs('data', exist_ok=True)
        fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        Config._init_database(self.db_path)
        conn = sqlite3.connect(self.db_path)
        have = {r[1] for r in conn.execute('PRAGMA table_info(series)')}
        for column in ('local_genre', 'manual_genre', 'bedetheque_genre', 'local_cover_path', 'bedetheque_cover_path',
                       'komga_cover_path', 'manual_summary', 'bedetheque_description', 'local_summary', 'manual_author',
                       'bedetheque_scenaristes', 'local_author', 'bedetheque_dessinateurs', 'manual_status',
                       'bedetheque_status', 'added_at', 'is_oneshot'):
            if column not in have:
                conn.execute(f'ALTER TABLE series ADD COLUMN {column} TEXT')
        conn.execute("INSERT INTO libraries (id, name, path) VALUES (1, 'BD', '/BD'), (2, 'Adultes', '/A')")
        conn.execute("INSERT INTO series (id, library_id, title) VALUES (1, 1, 'Visible'), (2, 2, 'Cachée')")
        conn.execute("INSERT INTO volumes (id, series_id, filepath) VALUES (1, 1, '/BD/a.cbz'), (2, 2, '/A/b.cbz')")
        for name, role, libs in (('alice', 'admin', None), ('bob', 'reader', '[1]')):
            conn.execute("INSERT INTO users (user_key, name, auth_method, password_hash, role_override, allowed_libraries_json) "
                         "VALUES (?, ?, 'password', ?, ?, ?)",
                         (f'password:{name}', name.title(), generate_password_hash('motdepasse1'), role, libs))
        conn.commit()
        conn.close()
        self.config_dir = tempfile.TemporaryDirectory()
        config_path = os.path.join(self.config_dir.name, 'oidc_config.json')
        with open(config_path, 'w') as f:
            json.dump({'mode': 'auth'}, f)
        from app import create_app
        self.app = create_app()
        self.app.config.update(DATABASE=self.db_path, TESTING=True, OIDC_CONFIG_FILE=config_path,
                               AUTH_BYPASS_LOGIN=False)

    def tearDown(self):
        os.remove(self.db_path)
        self.config_dir.cleanup()

    def _session_client(self, name):
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess['user'] = {'name': name, 'auth_method': 'password'}
            sess['csrf_token'] = 'tok'
        return client

    def _key_for(self, name):
        response = self._session_client(name).post('/api/auth/profile/api-key', headers={'X-CSRF-Token': 'tok'})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()['api_key']

    def test_profile_key_lifecycle(self):
        client = self._session_client('bob')
        self.assertEqual(client.get('/api/auth/profile/api-key').get_json()['api_key'], '')
        key = self._key_for('bob')
        self.assertEqual(len(key), 32)
        self.assertEqual(client.get('/api/auth/profile/api-key').get_json()['api_key'], key)
        stored = sqlite3.connect(self.db_path).execute(
            "SELECT api_key_hash, api_key_enc FROM users WHERE user_key = 'password:bob'").fetchone()
        self.assertNotIn(key, ''.join(stored))
        external = self.app.test_client()
        self.assertEqual(external.get('/api/auth/me', headers={'X-Api-Key': key}).get_json()['user']['name'], 'bob')
        self.assertEqual(client.delete('/api/auth/profile/api-key', headers={'X-CSRF-Token': 'tok'}).status_code, 200)
        self.assertEqual(external.get('/api/auth/me', headers={'X-Api-Key': key}).status_code, 401)

    def test_reader_key_keeps_reader_limits(self):
        key = self._key_for('bob')
        client = self.app.test_client()
        headers = {'X-Api-Key': key}
        self.assertEqual(client.get('/api/auth/me', headers=headers).get_json()['role'], 'reader')
        # La base de test n'a pas toutes les colonnes de la fiche : seul le contrôle d'accès compte.
        self.assertNotIn(client.get('/api/series/1', headers=headers).status_code, (401, 403, 404))
        self.assertEqual(client.get('/api/series/2', headers=headers).status_code, 404)
        catalog = client.get('/api/catalog/series', headers=headers).get_json()
        self.assertEqual([s['id'] for s in catalog['series']], [1])
        self.assertEqual(client.get('/api/auth/users', headers=headers).status_code, 403)

    def test_admin_key_is_admin_but_cannot_manage_keys(self):
        key = self._key_for('alice')
        client = self.app.test_client()
        headers = {'X-Api-Key': key}
        self.assertEqual(client.get('/api/auth/users', headers=headers).status_code, 200)
        self.assertEqual(client.get('/api/auth/api-key', headers=headers).status_code, 403)
        self.assertEqual(client.post('/api/auth/profile/api-key', headers=headers).status_code, 403)

    def test_token_endpoint_returns_personal_key(self):
        client = self.app.test_client()
        bad = client.post('/api/auth/token', json={'username': 'bob', 'password': 'faux'})
        self.assertEqual(bad.status_code, 401)
        first = client.post('/api/auth/token', json={'username': 'bob', 'password': 'motdepasse1'})
        self.assertEqual(first.status_code, 200, first.get_data(as_text=True))
        key = first.get_json()['api_key']
        again = client.post('/api/auth/token', json={'username': 'bob', 'password': 'motdepasse1'}).get_json()['api_key']
        self.assertEqual(key, again)
        self.assertEqual(client.get('/api/auth/me', headers={'X-Api-Key': key}).get_json()['role'], 'reader')

    def test_wrong_key_still_rejected(self):
        response = self.app.test_client().get('/api/auth/me', headers={'X-Api-Key': 'nimportequoi'})
        self.assertEqual(response.status_code, 401)


if __name__ == '__main__':
    unittest.main()
