import json
import os
import sqlite3
import tempfile
import unittest

from werkzeug.security import generate_password_hash

from config import Config


class ApiKeyAccessTest(unittest.TestCase):
    """Clé API d'un compte : accès aux routes /api/ sans session ni CSRF, jamais aux pages."""

    def setUp(self):
        os.makedirs('data', exist_ok=True)
        fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        Config._init_database(self.db_path)
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO users (user_key, name, auth_method, password_hash, role_override) "
                     "VALUES ('password:admin', 'Admin', 'password', ?, 'admin')", (generate_password_hash('secret'),))
        conn.commit()
        conn.close()
        self.config_dir = tempfile.TemporaryDirectory()
        self.config_path = os.path.join(self.config_dir.name, 'oidc_config.json')
        with open(self.config_path, 'w') as f:
            json.dump({'mode': 'password', 'username': 'admin',
                       'password_hash': generate_password_hash('secret')}, f)
        from app import create_app
        self.app = create_app()
        self.app.config.update(DATABASE=self.db_path, TESTING=True, OIDC_CONFIG_FILE=self.config_path,
                               AUTH_BYPASS_LOGIN=False)
        self.client = self.app.test_client()

    def tearDown(self):
        os.remove(self.db_path)
        self.config_dir.cleanup()

    def _login(self):
        with self.client.session_transaction() as sess:
            sess['user'] = {'name': 'admin', 'auth_method': 'password'}
            sess['csrf_token'] = 'tok'

    def _create_key(self):
        self._login()
        response = self.client.post('/api/auth/profile/api-keys', json={'name': 'Script'},
                                    headers={'X-CSRF-Token': 'tok'})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        key = response.get_json()['api_key']
        self.assertEqual(len(key), 32)
        self.client = self.app.test_client()  # client externe, sans session
        return key

    def test_key_is_stored_encrypted(self):
        key = self._create_key()
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute('SELECT key_hash, key_enc FROM api_keys').fetchall()
        conn.close()
        self.assertEqual(len(rows), 1)
        self.assertNotIn(key, ' '.join(str(v) for v in rows[0]))
        with open(self.config_path) as f:
            self.assertNotIn('api_key', f.read())

    def test_valid_key_opens_api_routes_without_session_or_csrf(self):
        key = self._create_key()
        self.assertEqual(self.client.get('/api/auth/me', headers={'X-Api-Key': key}).status_code, 200)
        self.assertEqual(self.client.get('/api/auth/me?apikey=' + key).status_code, 200)
        self.assertEqual(self.client.get('/api/auth/me', headers={'Authorization': 'Bearer ' + key}).status_code, 200)
        # Une écriture passe sans jeton CSRF (la route répond, même si la requête est incomplète)
        response = self.client.post('/api/auth/test', json={}, headers={'X-Api-Key': key})
        self.assertNotEqual(response.status_code, 403)
        self.assertNotIn(response.status_code, (302, 401))

    def test_wrong_key_is_rejected_with_json(self):
        self._create_key()
        response = self.client.get('/api/auth/me', headers={'X-Api-Key': 'faux'})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()['error'], 'Clé API invalide')

    def test_key_does_not_open_pages(self):
        key = self._create_key()
        response = self.client.get('/settings?apikey=' + key)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response.headers['Location'])

    def test_key_cannot_manage_keys(self):
        key = self._create_key()
        for method, url in (('get', '/api/auth/profile/api-keys'), ('post', '/api/auth/profile/api-keys'),
                            ('delete', '/api/auth/profile/api-keys/1'), ('get', '/api/auth/api-key')):
            response = getattr(self.client, method)(url, headers={'X-Api-Key': key})
            self.assertIn(response.status_code, (403, 404), f'{method} {url}')
            self.assertNotEqual(response.status_code, 200)

    def test_api_client_without_key_gets_401_json(self):
        response = self.client.get('/api/auth/me', headers={'Accept': 'application/json'})
        self.assertEqual(response.status_code, 401)
        self.assertIn('X-Api-Key', response.get_json()['error'])

    def test_deleted_key_stops_working(self):
        key = self._create_key()
        self._login()
        key_id = self.client.get('/api/auth/profile/api-keys').get_json()['keys'][0]['id']
        response = self.client.delete(f'/api/auth/profile/api-keys/{key_id}', headers={'X-CSRF-Token': 'tok'})
        self.assertEqual(response.status_code, 200)
        external = self.app.test_client()
        self.assertEqual(external.get('/api/auth/me', headers={'X-Api-Key': key}).status_code, 401)

    def test_docs_list_api_routes(self):
        key = self._create_key()
        response = self.client.get('/api/docs?format=json', headers={'X-Api-Key': key})
        data = response.get_json()
        paths = {r['path'] for r in data['routes']}
        self.assertIn('/api/activity/download-control', paths)
        self.assertTrue(all(p.startswith('/api/') for p in paths))
        html = self.client.get('/api/docs?apikey=' + key, headers={'Accept': 'text/html'})
        self.assertEqual(html.status_code, 200)
        self.assertIn('/api/activity/download-control', html.get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
