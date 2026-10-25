import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class NouveautesZapAndOwnedTest(unittest.TestCase):
    """Éclair de Nouveautés = toute la série ; « en bibliothèque » = logo Bullarr en vert."""

    def setUp(self):
        self.nouveautes = (ROOT / 'static/js/ebdz-latest.js').read_text()
        self.search_table = (ROOT / 'static/js/search-results-table.js').read_text()
        self.icons = (ROOT / 'static/js/icons.js').read_text()
        self.css = (ROOT / 'static/css/style-search.css').read_text()

    def test_zap_searches_every_missing_volume_of_the_series(self):
        start = self.nouveautes.index('async function addSeriesAndDownloadNouveautes(')
        body = self.nouveautes[start:self.nouveautes.index('async function _attachSeriesToPendingDownload(', start)]
        self.assertIn('_startSeriesAutoAcquire(seriesId)', body)
        self.assertNotIn('_downloadNouveautesRelease(', body)
        helper = body[body.index('async function _startSeriesAutoAcquire('):]
        self.assertIn("/api/bedetheque/auto-acquire/run", helper)
        self.assertIn('series_id: seriesId', helper)
        self.assertNotIn('volume_number', helper)  # série entière, pas un tome

    def test_zap_tooltips_say_the_whole_series(self):
        self.assertNotIn('télécharger cette release', self.nouveautes)
        self.assertNotIn('télécharger ce fichier"', self.nouveautes.replace('Télécharger et remplacer ce fichier', ''))
        self.assertGreaterEqual(self.nouveautes.count('télécharger tous ses tomes manquants'), 3)

    def test_library_pill_uses_the_bullarr_logo_and_is_green(self):
        self.assertIn('function libraryIconHtml()', self.icons)
        self.assertIn('/static/img/bullarr-icon.png', self.icons)
        self.assertIn('libraryIconHtml()', self.search_table)
        self.assertGreaterEqual(self.nouveautes.count('${libraryIconHtml()}'), 4)
        rule = self.css[self.css.index('.icon-owned {'):].split('}')[0]
        self.assertIn('#2e9e5b', rule)
        self.assertNotIn('border: 1px', rule)  # pas de pastille autour de l'icône
        self.assertNotIn('background', rule)

    def test_owned_rss_release_gets_the_green_row_like_ebdz_and_telegram(self):
        start = self.nouveautes.index('function _nouveautesRssRowHtml(')
        body = self.nouveautes[start:self.nouveautes.index('function _nouveautesTelegramRowHtml(', start)]
        self.assertIn("event.already_owned === true ? 'volume-table-row-owned '", body)


if __name__ == '__main__':
    unittest.main()
