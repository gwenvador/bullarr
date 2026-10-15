import unittest
from unittest.mock import MagicMock, patch

from flask import Flask

from blueprints.qbittorrent import qbittorrent_bp
from blueprints.qbittorrent import routes


def _response(redirect_to=None):
    response = MagicMock()
    response.is_redirect = bool(redirect_to)
    response.headers = {'Location': redirect_to} if redirect_to else {}
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    return response


class ProwlarrTorrentFileDownloadTest(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.register_blueprint(qbittorrent_bp)
        self.client = app.test_client()
        self.link = 'https://prowlarr.example/17/download?link=abc'

    def test_keyless_rss_link_is_fetched_with_configured_key(self):
        with patch.object(routes, '_configured_prowlarr_download_url', return_value=self.link + '&apikey=k'), \
             patch.object(routes.requests, 'get', return_value=_response()), \
             patch.object(routes, '_download_torrent_bytes', return_value=b'd8:announce') as fetch:
            response = self.client.get('/api/qbittorrent/torrent-file',
                                       query_string={'url': self.link, 'name': 'Les Pompiers T01'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b'd8:announce')
        self.assertIn('Les Pompiers T01.torrent', response.headers['Content-Disposition'])
        fetch.assert_called_once_with(self.link, 'prowlarr')

    def test_magnet_redirect_is_passed_to_browser(self):
        with patch.object(routes, '_configured_prowlarr_download_url', return_value=self.link),\
             patch.object(routes.requests, 'get', return_value=_response('magnet:?xt=urn:btih:abc')):
            response = self.client.get('/api/qbittorrent/torrent-file', query_string={'url': self.link})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers['Location'].startswith('magnet:'))

    def test_foreign_link_is_refused(self):
        with patch.object(routes, '_configured_prowlarr_download_url', return_value=None):
            response = self.client.get('/api/qbittorrent/torrent-file',
                                       query_string={'url': 'https://evil.example/1/download'})
        self.assertEqual(response.status_code, 400)


if __name__ == '__main__':
    unittest.main()
