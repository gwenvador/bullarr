import unittest
from pathlib import Path


class NouveautesRssClientsTest(unittest.TestCase):
    def setUp(self):
        source = (Path(__file__).resolve().parents[1] / 'static/js/ebdz-latest.js').read_text()
        self.source = source
        start = source.index('function _nouveautesRssTorrentClientButtons')
        end = source.index('function _nouveautesRssRowHtml', start)
        self.helper = source[start:end]
        self.row = source[end:source.index('function _nouveautesTelegramRowHtml', end)]
        self.nav = (Path(__file__).resolve().parents[1] / 'static/js/nav.js').read_text()

    def test_rss_origin_prefers_the_published_indexer_title(self):
        self.assertIn("e.feed_title || e.feed_name", self.source)

    def test_rss_torrent_buttons_use_enabled_clients(self):
        self.assertIn('enabledDownloadClients.qbittorrent', self.helper)
        self.assertIn('enabledDownloadClients.rtorrent', self.helper)
        self.assertIn('enabledDownloadClients.deluge', self.helper)
        self.assertIn('addTorrentToQbittorrent', self.helper)
        self.assertIn('addTorrentToRtorrent', self.helper)
        self.assertIn('addTorrentToDeluge', self.helper)
        self.assertIn("probe.includes('/download')", self.helper)

    def test_rss_row_renders_client_buttons_separately_from_open_link(self):
        self.assertIn('_nouveautesRssTorrentClientButtons(event, item, itemIndex)', self.row)
        self.assertIn('target="_blank"', self.row)

    def test_rss_qbittorrent_button_forwards_matched_series(self):
        helper_start = self.source.index('async function _addNouveautesRssTorrentToQbittorrent')
        helper_end = self.source.index('function _nouveautesRssTorrentClientButtons', helper_start)
        helper = self.source[helper_start:helper_end]
        self.assertIn('const event = allNouveautesEvents[eventIndex];', helper)
        self.assertIn('event?.series_id ?? null', helper)
        self.assertLess(helper.index('const event ='), helper.index('addTorrentToQbittorrent'))

    def test_rss_rows_rerender_after_download_client_config_loads(self):
        self.assertIn("download-clients-ready", self.source)
        self.assertIn("download-clients-ready", self.nav)
        self.assertIn("refreshEnabledDownloadClients().then", self.nav)


if __name__ == '__main__':
    unittest.main()
