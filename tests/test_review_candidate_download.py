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
