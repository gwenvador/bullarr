import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class LoginAuthModesTest(unittest.TestCase):
    def test_config_exposes_three_authentication_modes(self):
        source = (ROOT / 'blueprints/auth/routes.py').read_text()
        self.assertIn("'mode'", source)
        self.assertIn("'none'", source)
        self.assertIn("'password'", source)
        self.assertIn("'oidc'", source)

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
import tempfile

from werkzeug.security import generate_password_hash


class LoginPageBehaviourTest(unittest.TestCase):
    """Page de connexion: formulaire et/ou bouton SSO, jamais d'autologin par défaut."""

    @classmethod
    def setUpClass(cls):
        os.makedirs('data', exist_ok=True)
        from app import create_app
        cls.app = create_app()
        cls.app.config['TESTING'] = True

    def setUp(self):
        self.config_dir = tempfile.TemporaryDirectory()
        self.config_path = os.path.join(self.config_dir.name, 'oidc_config.json')
        self.app.config['OIDC_CONFIG_FILE'] = self.config_path
        self.client = self.app.test_client()

    def tearDown(self):
        self.config_dir.cleanup()

    def write_config(self, **values):
        base = {'mode': 'none', 'username': 'admin', 'password_hash': generate_password_hash('secret-pass'),
                'issuer': 'https://sso.example.test/realms/main', 'client_id': 'bullarr'}
        base.update(values)
        with open(self.config_path, 'w', encoding='utf-8') as handle:
            json.dump(base, handle)

    def page(self, path='/login'):
        response = self.client.get(path)
        return response, response.get_data(as_text=True)

    def test_mode_none_has_no_login_page(self):
        self.write_config(mode='none')
        response, _ = self.page()
        self.assertEqual((response.status_code, response.headers['Location']), (302, '/'))

    def test_password_mode_shows_the_form_only(self):
        self.write_config(mode='password')
        response, html = self.page()
        self.assertEqual(response.status_code, 200)
        self.assertIn('name="username"', html)
        self.assertNotIn('Se connecter avec le SSO', html)

    def test_oidc_mode_shows_a_button_and_does_not_auto_login(self):
        self.write_config(mode='oidc')
        response, html = self.page()
        self.assertEqual(response.status_code, 200)
        self.assertIn('href="/login/redirect"', html)
        self.assertIn('Se connecter avec le SSO', html)
        self.assertNotIn('name="username"', html)
        self.assertNotIn('window.location.replace', html)

    def test_both_mode_shows_the_form_and_the_sso_button(self):
        self.write_config(mode='both')
        response, html = self.page()
        self.assertEqual(response.status_code, 200)
        self.assertIn('name="username"', html)
        self.assertIn('Se connecter avec le SSO', html)
        self.assertIn('login-divider', html)

    def test_legacy_enabled_flag_still_means_oidc_without_auto_login(self):
        with open(self.config_path, 'w', encoding='utf-8') as handle:
            json.dump({'enabled': True, 'issuer': 'https://sso.example.test', 'client_id': 'bullarr'}, handle)
        response, html = self.page()
        self.assertEqual(response.status_code, 200)
        self.assertIn('Se connecter avec le SSO', html)

    def test_auto_redirect_is_opt_in_and_can_be_bypassed(self):
        self.write_config(mode='oidc', auto_redirect=True)
        response, _ = self.page()
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers['Location'].endswith('/login/redirect'))
        response, html = self.page('/login?noauto=1')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Se connecter avec le SSO', html)

    def test_incomplete_oidc_in_both_mode_still_offers_the_password_form(self):
        self.write_config(mode='both', issuer='', client_id='')
        response, html = self.page()
        self.assertEqual(response.status_code, 200)
        self.assertIn('name="username"', html)
        self.assertNotIn('Se connecter avec le SSO', html)
        self.assertIn('Configuration OIDC incomplète', html)

    def test_password_login_works_in_both_mode_and_is_refused_in_oidc_mode(self):
        self.write_config(mode='both')
        ok = self.client.post('/login/password', data={'username': 'admin', 'password': 'secret-pass'})
        self.assertEqual(ok.status_code, 302)
        with self.client.session_transaction() as session:
            self.assertEqual(session['user']['name'], 'admin')
        bad = self.app.test_client().post('/login/password', data={'username': 'admin', 'password': 'wrong'})
        self.assertEqual(bad.status_code, 401)
        self.assertIn('Se connecter avec le SSO', bad.get_data(as_text=True))
        self.write_config(mode='oidc')
        refused = self.app.test_client().post('/login/password', data={'username': 'admin', 'password': 'secret-pass'})
        self.assertEqual(refused.status_code, 401)

    def test_sso_endpoints_are_inert_when_sso_is_not_enabled(self):
        self.write_config(mode='password')
        response = self.client.get('/login/redirect')
        self.assertEqual((response.status_code, response.headers['Location']), (302, '/'))

    def test_unauthenticated_pages_redirect_to_login_in_both_mode(self):
        self.write_config(mode='both')
        response = self.client.get('/')
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers['Location'].endswith('/login'))

    def _authenticated_client(self):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session['user'] = {'name': 'admin'}
            session['csrf_token'] = 'token'
        return client

    def test_config_api_accepts_both_mode_and_persists_auto_redirect(self):
        self.write_config(mode='password')
        client = self._authenticated_client()
        payload = {'mode': 'both', 'username': 'admin', 'password': '', 'issuer': 'https://sso.example.test/',
                   'client_id': 'bullarr', 'scopes': 'openid email', 'auto_redirect': True}
        response = client.post('/api/auth/config', json=payload, headers={'X-CSRF-Token': 'token'})
        self.assertEqual(response.get_json(), {'success': True})
        config = client.get('/api/auth/config').get_json()
        self.assertEqual((config['mode'], config['auto_redirect'], config['issuer']),
                         ('both', True, 'https://sso.example.test'))

    def test_config_api_refuses_incomplete_modes(self):
        self.write_config(mode='password', password_hash='', username='')
        client = self._authenticated_client()
        headers = {'X-CSRF-Token': 'token'}
        no_password = client.post('/api/auth/config', headers=headers,
                                  json={'mode': 'both', 'username': 'admin', 'issuer': 'https://sso.example.test', 'client_id': 'x'})
        self.assertEqual(no_password.status_code, 400)
        no_client = client.post('/api/auth/config', headers=headers,
                                json={'mode': 'oidc', 'issuer': 'https://sso.example.test', 'client_id': ''})
        self.assertEqual(no_client.status_code, 400)
        unknown = client.post('/api/auth/config', headers=headers, json={'mode': 'magic'})
        self.assertEqual(unknown.status_code, 400)

    def test_config_api_defaults_auto_redirect_to_off(self):
        self.write_config(mode='oidc')
        config = self._authenticated_client().get('/api/auth/config').get_json()
        self.assertFalse(config['auto_redirect'])


if __name__ == '__main__':
    unittest.main()
