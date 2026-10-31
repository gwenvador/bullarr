import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class NouveautesPersistenceTest(unittest.TestCase):
    """Nouveautés garde ses dernières entrées et leurs origines: pas de recalcul ni d'appel
    Bédéthèque à chaque visite."""

    def setUp(self):
        self.script = (ROOT / 'static/js/ebdz-latest.js').read_text()

    def test_origins_are_kept_in_the_browser(self):
        self.assertIn("bullarr:nouveautes:origins:v1", self.script)
        self.assertIn('_saveNouveautesOrigins();', self.script)

    def test_cached_rss_rows_are_shown_at_once(self):
        start = self.script.index('function _restoreNouveautesCache(')
        body = self.script[start:self.script.index('function _mergeNouveautesClientState(', start)]
        self.assertNotIn("event.type !== 'rss'", body)

    def test_first_hundred_entries_are_loaded(self):
        self.assertIn('const NOUVEAUTES_INITIAL_LIMIT = 100;', self.script)


    def test_pending_files_come_from_the_database_not_a_disk_scan(self):
        start = self.script.index('async function loadNouveautesEvents(')
        body = self.script[start:self.script.index('function _nouveautesEventKey(', start)]
        self.assertIn("fetch('/api/import/pending-count')", body)
        self.assertNotIn("fetch('/api/import/scan'", body)


class TorrentLinkCheckTest(unittest.TestCase):
    def test_non_torrent_content_is_rejected_with_a_clear_message(self):
        source = (ROOT / 'blueprints/qbittorrent/routes.py').read_text()
        self.assertIn("torrent_bytes[:1] == b'd' and b'4:info' in torrent_bytes", source)
        self.assertIn("Le lien n'est pas un fichier torrent", source)


if __name__ == '__main__':
    unittest.main()
