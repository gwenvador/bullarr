import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from flask import Flask

from blueprints.ebdz import routes
from blueprints.missing_monitor import new_file_handler as handler

TITLE = "Blake.et.Mortimer.T31.La.Menace.atlante.Sente.Van.Dongen.Edition.Bibliophile.2025.FR.[CBZ]-example"


class RssTorrentUrlTest(unittest.TestCase):
    def test_direct_link_from_the_feed_is_used(self):
        item = {"filename": TITLE, "download_links": [{"url": "https://t.example/x.torrent"}]}
        self.assertEqual(handler._rss_torrent_url(item), "https://t.example/x.torrent")

    def test_exact_release_is_found_through_prowlarr(self):
        item = {"filename": TITLE, "link": "https://tracker.example/t/31", "download_links": [], "prowlarr_indexer_id": 17}
        results = [{"title": TITLE, "info_url": "https://tracker.example/t/other", "download_url": "https://dl/other"},
                   {"title": TITLE, "info_url": "https://tracker.example/t/31", "download_url": "https://dl/31"}]
        def search(query, *args, **kwargs):
            # Prowlarr ne retrouve la release que par le nom de série seul (cas réel).
            return results if query == "Blake et Mortimer" else []

        with mock.patch("blueprints.prowlarr.search.search_prowlarr_raw", side_effect=search) as searched:
            self.assertEqual(handler._rss_torrent_url(item, "Blake et Mortimer (Les Aventures de)"), "https://dl/31")
        self.assertEqual(searched.call_args_list[0].args[0], "Blake et Mortimer")
        with mock.patch("blueprints.prowlarr.search.search_prowlarr_raw", return_value=[]):
            self.assertIsNone(handler._rss_torrent_url(item))

    def test_rss_download_goes_to_the_torrent_client_with_the_series(self):
        downloader = mock.Mock()
        downloader.send_torrent_download.return_value = (True, "ok")
        item = {"filename": TITLE, "link": "https://tracker.example/t/31", "download_links": [{"url": "magnet:?xt=1"}]}
        handler._send_monitor_download("rss", item, downloader, "Blake et Mortimer", 31, 715, TITLE)
        args, kwargs = downloader.send_torrent_download.call_args
        self.assertEqual(args[:3], ("magnet:?xt=1", "Blake et Mortimer", 31))
        self.assertEqual((kwargs["series_id"], kwargs["source"], kwargs["source_link"]), (715, "prowlarr", "https://tracker.example/t/31"))


class PollRssTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = Flask(__name__)
        self.app.config["RSS_CONFIG_FILE"] = os.path.join(self.tmp.name, "rss.json")
        now = datetime.now(timezone.utc)
        self.entries = [
            {"title": TITLE, "link": "https://tracker.example/t/31", "date": now.isoformat()},
            {"title": "Vieille release", "link": "https://tracker.example/t/1", "date": (now - timedelta(days=10)).isoformat()},
        ]

    def poll(self, stats=None):
        with self.app.app_context(), \
                mock.patch.object(routes, "_prepared_rss_feeds", return_value=[{"name": "C411"}]), \
                mock.patch.object(routes, "_read_rss_feed", side_effect=lambda feed, path, *a: (feed, {"entries": list(self.entries)}, None)), \
                mock.patch("blueprints.missing_monitor.new_file_handler.process_new_monitor_files", return_value=stats or {"downloads_sent": 1}) as process:
            routes.poll_rss_for_monitoring()
        return process

    def test_first_run_only_takes_recent_releases_then_each_release_once(self):
        process = self.poll()
        source, items = process.call_args.args
        self.assertEqual(source, "rss")
        self.assertEqual([i["filename"] for i in items], [TITLE])               # la vieille release est ignorée
        self.assertEqual(items[0]["link"], "https://tracker.example/t/31")
        process = self.poll()
        process.assert_not_called()                                              # déjà vue
        self.entries.append({"title": "Nouvelle", "link": "https://tracker.example/t/2", "date": "2001-01-01T00:00:00+00:00"})
        process = self.poll()
        self.assertEqual([i["filename"] for i in process.call_args.args[1]], ["Nouvelle"])   # après le 1er passage: toute nouveauté

    def test_failed_release_is_retried_a_few_times_then_dropped(self):
        failure = {"downloads_sent": 0, "errors": ["Lien de téléchargement introuvable"]}
        self.poll(failure)
        calls = 1
        for _ in range(routes.RSS_MONITOR_MAX_RETRIES + 2):
            if self.poll(failure).called:
                calls += 1
        self.assertEqual(calls, routes.RSS_MONITOR_MAX_RETRIES)

    def test_old_state_format_restarts_with_a_first_run(self):
        with self.app.app_context():
            state = os.path.join(os.path.dirname(routes._rss_persistent_cache_path()), "rss_monitor_seen.json")
        with open(state, "w") as handle:
            json.dump(["ancienne-clé"], handle)
        process = self.poll()
        self.assertEqual([i["filename"] for i in process.call_args.args[1]], [TITLE])


if __name__ == "__main__":
    unittest.main()
