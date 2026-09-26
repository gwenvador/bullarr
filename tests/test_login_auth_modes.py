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

if __name__ == '__main__':
    unittest.main()
