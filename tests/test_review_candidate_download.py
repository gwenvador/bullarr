import unittest
from unittest import mock

from flask import Flask

from blueprints.bedetheque import auto_acquire


class ReviewCandidateDownloadTest(unittest.TestCase):
    """Un candidat de validation sans lien (entrée RSS) est retrouvé chez Prowlarr au clic."""

    def setUp(self):
        self.app = Flask(__name__)
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()

    def _download(self, candidate, resolved):
        if not hasattr(auto_acquire._rss_cached_download_url, "assert_called"):
            patcher = mock.patch.object(auto_acquire, "_rss_cached_download_url", return_value=None)
            patcher.start()
            self.addCleanup(patcher.stop)
        with mock.patch("blueprints.missing_monitor.downloader.find_pending_download_duplicate", return_value=None),              mock.patch("blueprints.missing_monitor.new_file_handler._rss_torrent_url", return_value=resolved) as lookup,              mock.patch("blueprints.missing_monitor.downloader.MissingVolumeDownloader") as downloader:
            downloader.return_value.send_torrent_download.return_value = (True, "ok")
            result = auto_acquire._download_result(self.app, candidate, 7, "Karabulut", None)
        return result, lookup, downloader.return_value.send_torrent_download

    def test_prowlarr_candidate_without_link_is_resolved(self):
        candidate = {"source": "prowlarr", "title": "Jusqu.ici.tout.allait.bien.Karabulut", "filename": "Jusqu.ici.tout.allait.bien.Karabulut",
                     "info_url": "https://tracker.example/t/1"}
        result, lookup, send = self._download(candidate, "https://prowlarr.example/17/download?link=x")
        self.assertEqual(result, (True, "ok"))
        self.assertEqual(lookup.call_args.args[0]["link"], "https://tracker.example/t/1")
        self.assertEqual(send.call_args.args[0], "https://prowlarr.example/17/download?link=x")

    def test_unresolvable_candidate_reports_no_link(self):
        candidate = {"source": "prowlarr", "title": "X", "filename": "X", "info_url": ""}
        result, _lookup, send = self._download(candidate, None)
        self.assertEqual(result, (False, "Aucun lien de téléchargement"))
        send.assert_not_called()

    def test_candidate_with_a_link_is_not_searched(self):
        candidate = {"source": "prowlarr", "title": "X", "filename": "X", "link": "https://prowlarr.example/17/download?link=y"}
        result, lookup, send = self._download(candidate, "unused")
        self.assertEqual(result, (True, "ok"))
        lookup.assert_not_called()


class RssPageLinkCandidateTest(ReviewCandidateDownloadTest):
    """Un candidat RSS dont le lien est la page de la release (pas un .torrent) est résolu chez Prowlarr."""

    def test_rss_page_link_is_resolved(self):
        candidate = {"source": "rss", "filename": "Les.Pompiers.HS.FR.[PDF]-NOTAG", "link": "https://c411.org/torrents/9f43"}
        result, lookup, send = self._download(candidate, "https://prowlarr.example/9/download?link=z")
        self.assertEqual(result, (True, "ok"))
        self.assertEqual(lookup.call_args.args[0]["link"], "https://c411.org/torrents/9f43")
        self.assertEqual(send.call_args.args[0], "https://prowlarr.example/9/download?link=z")

    def test_rss_page_link_not_found_is_not_sent(self):
        candidate = {"source": "rss", "filename": "X", "link": "https://c411.org/torrents/9f43"}
        result, _lookup, send = self._download(candidate, None)
        self.assertFalse(result[0])
        self.assertIn("introuvable chez Prowlarr", result[1])
        send.assert_not_called()

    def test_rss_direct_link_is_sent_as_is(self):
        candidate = {"source": "rss", "filename": "X", "link": "https://t.example/x.torrent"}
        result, lookup, send = self._download(candidate, "unused")
        self.assertEqual(result, (True, "ok"))
        lookup.assert_not_called()


class RssStoredDirectLinkTest(ReviewCandidateDownloadTest):
    """Le lien Prowlarr direct du flux est envoyé tel quel, sans recherche."""

    def test_stored_download_url_is_sent_without_search(self):
        candidate = {"source": "rss", "filename": "X", "link": "https://c411.org/torrents/9f43",
                     "download_url": "https://prowlarr.example/17/download?link=a"}
        result, lookup, send = self._download(candidate, "unused")
        self.assertEqual(result, (True, "ok"))
        lookup.assert_not_called()
        self.assertEqual(send.call_args.args[0], "https://prowlarr.example/17/download?link=a")

    def test_cached_rss_link_is_used_for_old_reviews(self):
        candidate = {"source": "rss", "filename": "X", "link": "https://c411.org/torrents/9f43"}
        with mock.patch.object(auto_acquire, "_rss_cached_download_url", return_value="https://prowlarr.example/17/download?link=b"):
            result, lookup, send = self._download(candidate, "unused")
        self.assertEqual(result, (True, "ok"))
        lookup.assert_not_called()
        self.assertEqual(send.call_args.args[0], "https://prowlarr.example/17/download?link=b")

    def test_review_candidate_keeps_direct_link(self):
        from blueprints.missing_monitor import new_file_handler as handler
        item = {"filename": "X", "link": "https://c411.org/torrents/9f43",
                "download_links": [{"url": "https://prowlarr.example/17/download?link=c"}]}
        c = handler._review_candidate("rss", item)
        self.assertEqual(c["download_url"], "https://prowlarr.example/17/download?link=c")
        self.assertEqual(c["info_url"], "https://c411.org/torrents/9f43")


class RssSentAsProwlarrTest(ReviewCandidateDownloadTest):
    def test_rss_link_is_sent_with_prowlarr_source(self):
        candidate = {"source": "rss", "filename": "X", "link": "https://c411.org/torrents/9f43",
                     "download_url": "https://prowlarr.example/17/download?link=a"}
        _result, _lookup, send = self._download(candidate, "unused")
        self.assertEqual(send.call_args.kwargs["source"], "prowlarr")
