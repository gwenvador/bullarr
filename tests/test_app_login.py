import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from flask import redirect

from blueprints.auth import routes as auth_routes


class AppLoginTest(unittest.TestCase):
    """Connexion SSO de l'application Android : navigateur du téléphone, code à usage unique, session."""

    @classmethod
    def setUpClass(cls):
        os.makedirs('data', exist_ok=True)
        from app import create_app
        cls.app = create_app()
        cls.app.config['TESTING'] = True

    def setUp(self):
        self.config_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.config_dir.cleanup)
        self.config_path = os.path.join(self.config_dir.name, 'oidc_config.json')
        self.app.config['OIDC_CONFIG_FILE'] = self.config_path
        self.write_config(mode='oidc')
        self.client = self.app.test_client()
        auth_routes._app_login_codes.clear()
        auth_routes._app_login_failures.clear()

    def write_config(self, **values):
        base = {'mode': 'oidc', 'issuer': 'https://sso.example.test', 'client_id': 'bullarr'}
        base.update(values)
        with open(self.config_path, 'w', encoding='utf-8') as handle:
            json.dump(base, handle)

    def _fake_oidc(self, userinfo=None):
        client = MagicMock()
        client.authorize_redirect.side_effect = lambda uri: redirect('https://sso.example.test/authorize')
        client.authorize_access_token.return_value = {
            'userinfo': userinfo or {'sub': 'u1', 'email': 'a@example.test', 'name': 'Alice'}}
        return patch.object(auth_routes, '_get_oidc_client', return_value=client)

    def _app_login_code(self):
        with self._fake_oidc():
            self.client.get('/login/redirect?app=1')
            response = self.client.get('/callback')
        self.assertEqual(response.status_code, 302)
        location = response.headers['Location']
        self.assertTrue(location.startswith('bullarr://auth?code='), location)
        return location.split('code=', 1)[1]

    def test_callback_sends_a_one_time_code_to_the_fixed_app_address_without_a_browser_session(self):
        code = self._app_login_code()
        self.assertGreaterEqual(len(code), 40)
        with self.client.session_transaction() as sess:
            self.assertNotIn('user', sess)
            self.assertNotIn('app_login', sess)

    def test_code_is_exchanged_once_for_a_session_with_csrf_token(self):
        code = self._app_login_code()
        app_client = self.app.test_client()
        response = app_client.post('/api/auth/app-login', json={'code': code})
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertTrue(body['success'])
        self.assertEqual(body['user']['name'], 'Alice')
        self.assertEqual(app_client.get('/api/auth/me').get_json()['user']['sub'], 'u1')
        self.assertTrue(app_client.get('/api/auth/csrf').get_json()['token'])
        again = self.app.test_client().post('/api/auth/app-login', json={'code': code})
        self.assertEqual(again.status_code, 400)

    def test_unknown_expired_or_missing_code_is_refused(self):
        client = self.app.test_client()
        for payload in ({'code': 'nope'}, {}, {'code': ''}, {'code': 123}):
            response = client.post('/api/auth/app-login', json=payload)
            self.assertEqual(response.status_code, 400, payload)
            self.assertEqual(response.get_json()['error'], 'Code invalide ou expiré')
        code = self._app_login_code()
        auth_routes._app_login_codes[code] = (0, auth_routes._app_login_codes[code][1])
        self.assertEqual(client.post('/api/auth/app-login', json={'code': code}).status_code, 400)

    def test_too_many_failures_are_rate_limited(self):
        client = self.app.test_client()
        for _ in range(auth_routes.APP_LOGIN_MAX_FAILURES_PER_MINUTE):
            self.assertEqual(client.post('/api/auth/app-login', json={'code': 'x'}).status_code, 400)
        self.assertEqual(client.post('/api/auth/app-login', json={'code': 'x'}).status_code, 429)

    def test_exchange_is_refused_when_sso_is_not_enabled(self):
        code = self._app_login_code()
        self.write_config(mode='password')
        response = self.app.test_client().post('/api/auth/app-login', json={'code': code})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['error'], 'Connexion SSO non activée')

    def test_normal_web_login_is_unchanged_and_the_app_flag_does_not_leak(self):
        with self._fake_oidc():
            self.client.get('/login/redirect?app=1')
            self.client.get('/login/redirect')  # connexion web ensuite : le drapeau est retiré
            response = self.client.get('/callback')
        self.assertFalse(response.headers['Location'].startswith('bullarr://'))
        with self.client.session_transaction() as sess:
            self.assertEqual(sess['user']['name'], 'Alice')

    def test_redirect_target_is_never_taken_from_the_request(self):
        with self._fake_oidc():
            self.client.get('/login/redirect?app=1&redirect_uri=https://evil.example&next=//evil.example')
            response = self.client.get('/callback?redirect_uri=https://evil.example')
        self.assertTrue(response.headers['Location'].startswith('bullarr://auth?code='))

    def test_pending_codes_are_capped(self):
        for index in range(auth_routes.APP_LOGIN_MAX_PENDING_CODES + 20):
            auth_routes._issue_app_login_code({'sub': str(index)})
        self.assertLessEqual(len(auth_routes._app_login_codes), auth_routes.APP_LOGIN_MAX_PENDING_CODES)


if __name__ == '__main__':
    unittest.main()
