import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from config import _env_flag


class EnvFlagTest(unittest.TestCase):
    def test_truthy_values_enable_the_flag(self):
        for value in ('1', 'true', 'TRUE', ' yes ', 'On'):
            with mock.patch.dict(os.environ, {'BULLARR_TEST_FLAG': value}):
                self.assertTrue(_env_flag('BULLARR_TEST_FLAG'), value)

    def test_anything_else_or_unset_keeps_it_off(self):
        for value in ('', 'false', '0', 'no', 'off', 'maybe'):
            with mock.patch.dict(os.environ, {'BULLARR_TEST_FLAG': value}):
                self.assertFalse(_env_flag('BULLARR_TEST_FLAG'), value)
        with mock.patch.dict(os.environ, clear=False):
            os.environ.pop('BULLARR_TEST_FLAG', None)
            self.assertFalse(_env_flag('BULLARR_TEST_FLAG'))

    def test_config_reads_the_documented_variable(self):
        root = Path(__file__).resolve().parents[1]
        self.assertIn("AUTH_BYPASS_LOGIN = _env_flag('BULLARR_AUTH_BYPASS_LOGIN')", (root / 'config.py').read_text())
        self.assertIn('BULLARR_AUTH_BYPASS_LOGIN', (root / 'docker-compose.example.yml').read_text())
        self.assertIn('BULLARR_AUTH_BYPASS_LOGIN', (root / 'README.md').read_text())


class BypassSwitchBehaviourTest(unittest.TestCase):
    """Un SSO mal configuré ne doit pas verrouiller l'accès: le commutateur doit fonctionner."""

    @classmethod
    def setUpClass(cls):
        os.makedirs('data', exist_ok=True)
        from app import create_app
        cls.app = create_app()
        cls.app.config['TESTING'] = True

    def setUp(self):
        self.config_dir = tempfile.TemporaryDirectory()
        path = os.path.join(self.config_dir.name, 'oidc_config.json')
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump({'mode': 'oidc', 'issuer': '', 'client_id': ''}, handle)  # SSO cassé
        self.app.config['OIDC_CONFIG_FILE'] = path
        self.client = self.app.test_client()

    def tearDown(self):
        self.app.config['AUTH_BYPASS_LOGIN'] = False
        self.config_dir.cleanup()

    def test_without_the_switch_a_broken_sso_locks_every_page(self):
        self.app.config['AUTH_BYPASS_LOGIN'] = False
        self.assertEqual(self.client.get('/api/auth/config').status_code, 302)

    def test_with_the_switch_the_configuration_can_be_reached_and_fixed(self):
        self.app.config['AUTH_BYPASS_LOGIN'] = True
        response = self.client.get('/api/auth/config')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['mode'], 'oidc')
        fixed = self.client.post('/api/auth/config', json={'mode': 'none'})
        self.assertEqual(fixed.get_json(), {'success': True})


if __name__ == '__main__':
    unittest.main()
