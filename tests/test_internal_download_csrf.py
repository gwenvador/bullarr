"""Internal client dispatch must pass the same CSRF gate as authenticated writes."""
import unittest
from unittest.mock import patch

from flask import Flask, jsonify

from blueprints.auth import auth_bp
from blueprints.missing_monitor.downloader import MissingVolumeDownloader


class InternalDownloadCsrfTests(unittest.TestCase):
    def test_internal_download_passes_csrf_check(self):
        app = Flask(__name__)
        app.config.update(TESTING=True, SECRET_KEY='test-only')
        app.register_blueprint(auth_bp)
        received = []

        def add():
            from flask import request
            received.append(request.get_json())
            return jsonify({'success': True})

        app.add_url_rule('/fake/add', 'fake_add', add, methods=['POST'])
        with patch('blueprints.auth.routes.load_oidc_config', return_value={'mode': 'password'}):
            with app.app_context():
                success, _ = MissingVolumeDownloader()._post_to_client(
                    'http://127.0.0.1:5000/fake/add', {'title': 'Album'},
                    'qbittorrent', 'qBittorrent', 'Album', None, None, None,
                )

        self.assertTrue(success)
        self.assertEqual(received, [{'title': 'Album'}])


if __name__ == '__main__':
    unittest.main()
