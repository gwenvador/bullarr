import unittest
from pathlib import Path


class NouveautesRssClientsTest(unittest.TestCase):
    def setUp(self):
        source = (Path(__file__).resolve().parents[1] / 'static/js/ebdz-latest.js').read_text()
        start = source.index('function _nouveautesRssTorrentClientButtons')
        end = source.index('function _nouveautesRssRowHtml', start)
        self.helper = source[start:end]
        self.row = source[end:source.index('function _nouveautesTelegramRowHtml', end)]

    def test_rss_torrent_buttons_use_enabled_clients(self):
        self.assertIn('enabledDownloadClients.qbittorrent', self.helper)
        self.assertIn('enabledDownloadClients.rtorrent', self.helper)
        self.assertIn('enabledDownloadClients.deluge', self.helper)
        self.assertIn('addTorrentToQbittorrent', self.helper)
        self.assertIn('addTorrentToRtorrent', self.helper)
        self.assertIn('addTorrentToDeluge', self.helper)

    def test_rss_row_renders_client_buttons_separately_from_open_link(self):
        self.assertIn('_nouveautesRssTorrentClientButtons(event, item)', self.row)
        self.assertIn('target="_blank"', self.row)


if __name__ == '__main__':
    unittest.main()
