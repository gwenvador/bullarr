"""Only configured Prowlarr download links may use a private address."""
import unittest
from unittest.mock import MagicMock, Mock, patch

from blueprints.qbittorrent.routes import (
    _download_torrent_bytes,
    _is_configured_prowlarr_download,
)


CONFIG = {
    'enabled': True,
    'url': 'https://prowlarr.example.test',
    'api_key_decrypted': 'test-key',
}


class ProwlarrTorrentDownloadTests(unittest.TestCase):
    @patch('blueprints.prowlarr.config_store.load_prowlarr_config', return_value=CONFIG)
    def test_trusted_link_requires_exact_origin_path_and_key(self, _config):
        valid = 'https://prowlarr.example.test/17/download?apikey=test-key&link=release'
        self.assertTrue(_is_configured_prowlarr_download(valid))
        self.assertFalse(_is_configured_prowlarr_download(valid.replace('test-key', 'wrong')))
        self.assertFalse(_is_configured_prowlarr_download(valid.replace('/17/download', '/api/v1/system/status')))
        self.assertFalse(_is_configured_prowlarr_download(valid.replace('prowlarr.example.test', 'other.example.test')))

    @patch('blueprints.prowlarr.config_store.load_prowlarr_config', return_value=CONFIG)
    def test_trusted_private_link_is_bounded_and_not_redirected(self, _config):
        response = MagicMock()
        response.__enter__.return_value = response
        response.is_redirect = False
        response.headers = {'Content-Length': '3'}
        response.iter_content.return_value = [b'abc']
        with patch('blueprints.qbittorrent.routes.requests.get', return_value=response) as get, \
             patch('blueprints.qbittorrent.routes.safe_external_get') as public_get:
            data = _download_torrent_bytes(
                'https://prowlarr.example.test/17/download?apikey=test-key', 'prowlarr'
            )
        self.assertEqual(data, b'abc')
        self.assertFalse(get.call_args.kwargs['allow_redirects'])
        public_get.assert_not_called()

    @patch('blueprints.prowlarr.config_store.load_prowlarr_config', return_value=CONFIG)
    def test_other_links_still_use_public_url_guard(self, _config):
        public_response = Mock(content=b'torrent')
        with patch('blueprints.qbittorrent.routes.safe_external_get', return_value=public_response) as public_get, \
             patch('blueprints.qbittorrent.routes.requests.get') as trusted_get:
            self.assertEqual(
                _download_torrent_bytes('https://other.example.test/file.torrent', 'prowlarr'), b'torrent'
            )
        public_get.assert_called_once()
        trusted_get.assert_not_called()


if __name__ == '__main__':
    unittest.main()
