import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class LoginAuthModesTest(unittest.TestCase):
    def test_config_exposes_the_two_authentication_modes(self):
        source = (ROOT / 'blueprints/auth/routes.py').read_text()
        self.assertIn("'mode'", source)
        self.assertIn("'none'", source)
        self.assertIn("'auth'", source)
        self.assertIn("oidc_enabled", source)

    def test_password_login_route_uses_hashed_password_check(self):
        source = (ROOT / 'blueprints/auth/routes.py').read_text()
        self.assertIn("@auth_bp.route('/login/password'", source)
        self.assertIn('check_password_hash', source)
        self.assertIn("session['user']", source)

    def test_login_page_is_named_login_and_has_password_form(self):
        html = (ROOT / 'templates/login.html').read_text()
        settings = (ROOT / 'templates/settings.html').read_text()
        js = (ROOT / 'static/js/settings.js').read_text()
        self.assertIn('<title>Login</title>', html)
        self.assertIn('name="username"', html)
        self.assertIn('name="password"', html)
        self.assertIn('Mode d’authentification', settings)
        self.assertIn('authMode', settings)
        self.assertIn('authMode', js)
        self.assertNotIn('8 caractères', (ROOT / 'blueprints/auth/routes.py').read_text())
        self.assertIn('auth-mode-options', settings)
        self.assertIn('auth-mode-option', settings)
        self.assertIn('login-form', html)
        self.assertIn('login-field', html)
        self.assertIn('ProxyFix', (ROOT / 'app.py').read_text())
        self.assertIn('x_proto=1', (ROOT / 'app.py').read_text())


import json
import os
import sqlite3
import tempfile

from werkzeug.security import generate_password_hash


class LoginPageBehaviourTest(unittest.TestCase):
    """Deux modes : sans authentification, ou authentification (comptes Bullarr, plus le
    bouton SSO si OIDC est activé). Jamais d'autologin par défaut."""

    @classmethod
    def setUpClass(cls):
        os.makedirs('data', exist_ok=True)
        from app import create_app
        cls.app = create_app()
        cls.app.config['TESTING'] = True

    def setUp(self):
        from blueprints.auth import routes
        from blueprints.auth.roles import init_tables
        routes._password_login_failures.clear()
        self.config_dir = tempfile.TemporaryDirectory()
        self.config_path = os.path.join(self.config_dir.name, 'oidc_config.json')
        self.db_path = os.path.join(self.config_dir.name, 'bullarr.db')
        conn = sqlite3.connect(self.db_path)
        init_tables(conn.cursor())
        conn.commit()
        conn.close()
        self.saved = {key: self.app.config.get(key) for key in ('OIDC_CONFIG_FILE', 'DATABASE')}
        self.app.config['OIDC_CONFIG_FILE'] = self.config_path
        self.app.config['DATABASE'] = self.db_path
        self.client = self.app.test_client()

    def tearDown(self):
        self.app.config.update(self.saved)
        self.config_dir.cleanup()

    def write_config(self, **values):
        base = {'mode': 'auth', 'oidc_enabled': False,
                'issuer': 'https://sso.example.test/realms/main', 'client_id': 'bullarr'}
        base.update(values)
        with open(self.config_path, 'w', encoding='utf-8') as handle:
            json.dump(base, handle)

    def saved_config(self):
        with open(self.config_path, encoding='utf-8') as handle:
            return json.load(handle)

    def add_account(self, username='admin', password='secret-pass', role='admin'):
        with self.app.app_context():
            from blueprints.auth import accounts
            return accounts.create_local_account(username, password, role=role)

    def page(self, path='/login'):
        response = self.client.get(path)
        return response, response.get_data(as_text=True)

    def login(self, client, username='admin', password='secret-pass'):
        return client.post('/login/password', data={'username': username, 'password': password})

    def test_mode_none_has_no_login_page(self):
        self.write_config(mode='none')
        response, _ = self.page()
        self.assertEqual((response.status_code, response.headers['Location']), (302, '/'))

    def test_auth_mode_without_oidc_shows_the_form_only(self):
        self.write_config()
        response, html = self.page()
        self.assertEqual(response.status_code, 200)
        self.assertIn('name="username"', html)
        self.assertNotIn('Se connecter avec le SSO', html)
        redirect_response = self.client.get('/login/redirect')
        self.assertEqual((redirect_response.status_code, redirect_response.headers['Location']), (302, '/'))

    def test_auth_mode_with_oidc_shows_the_form_and_the_sso_button(self):
        self.write_config(oidc_enabled=True)
        response, html = self.page()
        self.assertEqual(response.status_code, 200)
        self.assertIn('name="username"', html)
        self.assertIn('href="/login/redirect"', html)
        self.assertIn('Se connecter avec le SSO', html)
        self.assertIn('login-divider', html)
        self.assertNotIn('window.location.replace', html)

    def test_legacy_modes_become_auth(self):
        for legacy, oidc in (('password', False), ('oidc', True), ('both', True)):
            with self.subTest(legacy=legacy):
                with open(self.config_path, 'w', encoding='utf-8') as handle:
                    json.dump({'mode': legacy, 'issuer': 'https://sso.example.test', 'client_id': 'bullarr'}, handle)
                response, html = self.page()
                self.assertEqual(response.status_code, 200)
                self.assertIn('name="username"', html)
                self.assertEqual('Se connecter avec le SSO' in html, oidc)

    def test_legacy_single_account_is_migrated_and_still_logs_in(self):
        self.write_config(mode='both', username='admin', password_hash=generate_password_hash('secret-pass'))
        self.page()
        saved = self.saved_config()
        self.assertNotIn('password_hash', saved)
        self.assertNotIn('username', saved)
        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT auth_method, role_override, password_hash IS NOT NULL FROM users WHERE user_key = 'password:admin'").fetchone()
        conn.close()
        self.assertEqual(row, ('password', 'admin', 1))
        self.assertEqual(self.login(self.client).status_code, 302)
        with self.client.session_transaction() as session:
            self.assertEqual(session['user']['name'], 'admin')

    def test_legacy_account_already_seen_at_login_gets_its_password(self):
        # Les connexions d'avant créaient déjà la ligne users, sans mot de passe.
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO users (user_key, name, auth_method) VALUES ('password:admin', 'admin', 'password')")
        conn.commit()
        conn.close()
        self.write_config(mode='both', username='admin', password_hash=generate_password_hash('secret-pass'))
        self.page()
        self.assertEqual(self.login(self.client).status_code, 302)

    def test_legacy_enabled_flag_still_means_oidc_without_auto_login(self):
        with open(self.config_path, 'w', encoding='utf-8') as handle:
            json.dump({'enabled': True, 'issuer': 'https://sso.example.test', 'client_id': 'bullarr'}, handle)
        response, html = self.page()
        self.assertEqual(response.status_code, 200)
        self.assertIn('Se connecter avec le SSO', html)

    def test_auto_redirect_is_opt_in_and_can_be_bypassed(self):
        self.write_config(oidc_enabled=True, auto_redirect=True)
        response, _ = self.page()
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers['Location'].endswith('/login/redirect'))
        response, html = self.page('/login?noauto=1')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Se connecter avec le SSO', html)

    def test_incomplete_oidc_still_offers_the_password_form(self):
        self.write_config(oidc_enabled=True, issuer='', client_id='')
        response, html = self.page()
        self.assertEqual(response.status_code, 200)
        self.assertIn('name="username"', html)
        self.assertNotIn('Se connecter avec le SSO', html)
        self.assertIn('Configuration OIDC incomplète', html)

    def test_password_login_checks_the_account_and_is_off_without_auth(self):
        self.write_config()
        self.add_account()
        self.assertEqual(self.login(self.client).status_code, 302)
        bad = self.login(self.app.test_client(), password='wrong-pass')
        self.assertEqual(bad.status_code, 401)
        self.write_config(mode='none')
        refused = self.login(self.app.test_client())
        self.assertEqual(refused.status_code, 401)

    def test_unauthenticated_pages_redirect_to_login(self):
        self.write_config()
        response = self.client.get('/')
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers['Location'].endswith('/login'))

    def _admin_client(self):
        self.add_account()
        client = self.app.test_client()
        self.assertEqual(self.login(client).status_code, 302)
        with client.session_transaction() as session:
            session['csrf_token'] = 'token'
        return client

    def test_config_api_round_trips_auth_mode_and_oidc(self):
        self.write_config()
        client = self._admin_client()
        payload = {'mode': 'auth', 'oidc_enabled': True, 'issuer': 'https://sso.example.test/',
                   'client_id': 'bullarr', 'scopes': 'openid email', 'auto_redirect': True}
        response = client.post('/api/auth/config', json=payload, headers={'X-CSRF-Token': 'token'})
        self.assertEqual(response.get_json(), {'success': True})
        config = client.get('/api/auth/config').get_json()
        self.assertEqual((config['mode'], config['oidc_enabled'], config['auto_redirect'], config['issuer']),
                         ('auth', True, True, 'https://sso.example.test'))
        legacy = client.post('/api/auth/config', json=dict(payload, mode='both'), headers={'X-CSRF-Token': 'token'})
        self.assertEqual(legacy.status_code, 200)
        self.assertEqual(client.get('/api/auth/config').get_json()['mode'], 'auth')

    def test_config_api_refuses_incomplete_modes(self):
        self.write_config(mode='none')
        client = self.app.test_client()
        with client.session_transaction() as session:
            session['csrf_token'] = 'token'
        headers = {'X-CSRF-Token': 'token'}
        no_account = client.post('/api/auth/config', headers=headers, json={'mode': 'auth', 'oidc_enabled': False})
        self.assertEqual(no_account.status_code, 400)
        self.assertIn('compte administrateur', no_account.get_json()['error'])
        no_client = client.post('/api/auth/config', headers=headers,
                                json={'mode': 'auth', 'oidc_enabled': True, 'issuer': 'https://sso.example.test', 'client_id': ''})
        self.assertEqual(no_client.status_code, 400)
        unknown = client.post('/api/auth/config', headers=headers, json={'mode': 'magic'})
        self.assertEqual(unknown.status_code, 400)
        self.add_account()
        ok = client.post('/api/auth/config', headers=headers, json={'mode': 'auth', 'oidc_enabled': False})
        self.assertEqual(ok.status_code, 200)

    def test_config_api_defaults_auto_redirect_to_off(self):
        self.write_config(oidc_enabled=True)
        config = self._admin_client().get('/api/auth/config').get_json()
        self.assertFalse(config['auto_redirect'])

    # --- Comptes : création, édition, suppression ---

    def test_admin_creates_edits_and_deletes_a_local_account(self):
        self.write_config()
        admin = self._admin_client()
        headers = {'X-CSRF-Token': 'token'}
        created = admin.post('/api/auth/users', headers=headers,
                             json={'username': 'camille', 'password': 'lecture-123', 'name': 'Camille', 'role': 'reader'})
        self.assertEqual(created.status_code, 200, created.get_json())
        weak = admin.post('/api/auth/users', headers=headers, json={'username': 'court', 'password': 'abc'})
        self.assertEqual(weak.status_code, 400)
        duplicate = admin.post('/api/auth/users', headers=headers, json={'username': 'camille', 'password': 'lecture-123'})
        self.assertEqual(duplicate.status_code, 400)
        users = {u['key']: u for u in admin.get('/api/auth/users').get_json()['users']}
        self.assertEqual((users['password:camille']['type'], users['password:camille']['role'],
                          users['password:camille']['name']), ('local', 'reader', 'Camille'))
        self.assertNotIn('password_hash', json.dumps(users))

        reader = self.app.test_client()
        self.assertEqual(self.login(reader, 'camille', 'lecture-123').status_code, 302)
        self.assertEqual(reader.get('/settings').status_code, 302)
        self.assertEqual(reader.get('/api/auth/users').status_code, 403)

        edited = admin.put('/api/auth/users', headers=headers,
                           json={'key': 'password:camille', 'name': 'Camille D.', 'email': 'c@example.test',
                                 'password': 'nouveau-456', 'role': 'reader'})
        self.assertEqual(edited.status_code, 200, edited.get_json())
        # Changer le mot de passe ferme la session ouverte.
        self.assertEqual(reader.get('/').status_code, 302)
        self.assertEqual(self.login(self.app.test_client(), 'camille', 'lecture-123').status_code, 401)
        self.assertEqual(self.login(reader, 'camille', 'nouveau-456').status_code, 302)

        deleted = admin.delete('/api/auth/users', headers=headers, json={'key': 'password:camille'})
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(reader.get('/').status_code, 302)
        self.assertEqual(self.login(self.app.test_client(), 'camille', 'nouveau-456').status_code, 401)
        keys = [u['key'] for u in admin.get('/api/auth/users').get_json()['users']]
        self.assertNotIn('password:camille', keys)

    def test_admin_cannot_lock_everyone_out(self):
        self.write_config()
        admin = self._admin_client()
        headers = {'X-CSRF-Token': 'token'}
        self.assertEqual(admin.delete('/api/auth/users', headers=headers, json={'key': 'password:admin'}).status_code, 400)
        self.assertEqual(admin.put('/api/auth/users', headers=headers,
                                   json={'key': 'password:admin', 'role': 'reader'}).status_code, 400)
        self.add_account('second', 'second-pass', role='admin')
        second = self.app.test_client()
        self.login(second, 'second', 'second-pass')
        with second.session_transaction() as session:
            session['csrf_token'] = 'token'
        # Avec un second admin, le premier peut être supprimé par lui.
        self.assertEqual(second.delete('/api/auth/users', headers=headers, json={'key': 'password:admin'}).status_code, 200)
        # Le dernier admin local restant est protégé tant qu'OIDC est désactivé.
        self.add_account('third', 'third-pass', role='reader')
        self.assertEqual(second.put('/api/auth/users', headers=headers,
                                    json={'key': 'password:third', 'role': 'admin'}).status_code, 200)


if __name__ == '__main__':
    unittest.main()
