import unittest
from unittest import mock

import requests
from flask import Flask

from blueprints.bdgest import bdgest_bp
from blueprints.bedetheque.guard import BedethequeBlockedError


class SortiesErrorsTest(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.register_blueprint(bdgest_bp, url_prefix='/api/bdgest')
        self.client = app.test_client()

    def call(self, error):
        guard = mock.Mock()
        with mock.patch('blueprints.bedetheque.catalog_index.get_cached_scrape', return_value=None), \
                mock.patch('blueprints.bedetheque.guard.get_guard', return_value=guard), \
                mock.patch('blueprints.bdgest.routes._warmed_bdgest_session'), \
                mock.patch('blueprints.bdgest.routes._fetch_bdgest_page', side_effect=error):
            return self.client.get('/api/bdgest/sorties?categorie=bd')

    def test_guard_pause_is_reported_with_its_reason(self):
        guard_error = BedethequeBlockedError("Bédéthèque en pause jusqu'à 08:58 (HTTP 403 (défi Cloudflare))")
        response = self.call(guard_error)
        self.assertEqual(response.status_code, 503)
        self.assertIn('08:58', response.get_json()['error'])

    def test_cloudflare_refusal_is_not_an_internal_error(self):
        http_error = requests.exceptions.HTTPError(response=mock.Mock(status_code=403))
        response = self.call(http_error)
        self.assertEqual(response.status_code, 502)
        self.assertIn('HTTP 403', response.get_json()['error'])


if __name__ == '__main__':
    unittest.main()
