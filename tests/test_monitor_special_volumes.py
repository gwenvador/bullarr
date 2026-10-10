import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from flask import Flask

from blueprints.library import routes
from blueprints.missing_monitor import new_file_handler as handler

FILE = "Mortelle Adèle HS2020-10 - Mortelle Adèle et la galaxie des Bizarres (1584p).cbz"


class SpecialVolumesTest(unittest.TestCase):
    """Cas Mortelle Adèle: hors-séries numérotés par année, deux albums « HS 2020 »."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "bullarr.db")
        conn = sqlite3.connect(self.db)
        conn.executescript("""
            CREATE TABLE volumes (id INTEGER PRIMARY KEY, series_id INTEGER, volume_number INTEGER, is_bis INTEGER DEFAULT 0,
                is_integral INTEGER DEFAULT 0, integral_number INTEGER, is_hs INTEGER DEFAULT 0, hs_number INTEGER,
                is_episode INTEGER DEFAULT 0, episode_number INTEGER, is_special INTEGER DEFAULT 0, special_label TEXT,
                filepath TEXT, file_size INTEGER, format TEXT, comicinfo TEXT);
            CREATE TABLE active_downloads (id INTEGER PRIMARY KEY, series_id INTEGER, status TEXT, is_hs INTEGER DEFAULT 0,
                hs_number INTEGER, is_integral INTEGER DEFAULT 0, integral_number INTEGER, is_episode INTEGER DEFAULT 0, episode_number INTEGER);
        """)
        for vid, title in ((103359, "HS2020 . Fais ta BD avec Mortelle Adèle"), (103360, "HS2020/10 . Mortelle Adèle et la galaxie des Bizarres")):
            conn.execute("INSERT INTO volumes (id, series_id, is_hs, hs_number, comicinfo) VALUES (?, 499, 1, 2020, ?)", (vid, json.dumps({"title": title})))
        conn.execute("INSERT INTO volumes (id, series_id, is_hs, hs_number, filepath, comicinfo) VALUES (835033, 499, 1, 2019, \x27/BD/x.cbz\x27, \x27{}\x27)")
        conn.commit()
        conn.close()
        app = Flask(__name__)
        app.config["DATABASE"] = self.db
        self.ctx = app.app_context()
        self.ctx.push()
        self.addCleanup(self.ctx.pop)

    def cursor(self):
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        return conn.cursor()

    def test_title_picks_the_right_album_among_same_number(self):
        cur = self.cursor()
        self.assertEqual(routes.pick_volume_by_title(cur, [103359, 103360], FILE), 103360)
        self.assertEqual(routes.pick_volume_by_title(cur, [103359, 103360], "Fais ta BD avec Mortelle Adele HS2020.cbz"), 103359)
        self.assertIsNone(routes.pick_volume_by_title(cur, [103359, 103360], "Mortelle Adele HS2020.cbz"))

    def test_import_uses_the_file_name_to_choose_the_album(self):
        cur = self.cursor()
        parsed = {"is_hs": True, "hs_number": 2020}
        self.assertEqual(routes._find_existing_volume_for_import(cur, 499, parsed, source_name="Fais ta BD - HS2020.cbz")[0], 103359)
        self.assertEqual(routes._find_existing_volume_for_import(cur, 499, parsed, source_name=FILE)[0], 103360)

    def test_typed_identity_and_missing_album_lookup(self):
        self.assertEqual(handler._typed_identity({"is_hs": True, "hs_number": 2020})[:3], ("is_hs", "hs_number", 2020))
        self.assertIsNone(handler._typed_identity({"volume": 3}))
        cur = self.cursor()
        self.assertEqual(handler._missing_typed_volume(cur, 499, ("is_hs", "hs_number", 2020, "HS 2020"), FILE), (103360, False))
        self.assertEqual(handler._missing_typed_volume(cur, 499, ("is_hs", "hs_number", 2020, "HS 2020"), "HS2020.cbz"), (None, True))
        self.assertEqual(handler._missing_typed_volume(cur, 499, ("is_hs", "hs_number", 2019, "HS 2019"), FILE), (None, False))  # déjà possédé

    def run_typed(self, filename, title="Mortelle Adèle"):
        downloader = mock.Mock()
        downloader.send_torrent_download.return_value = (True, "envoyé")
        stats = {"queued_for_review": 0, "skipped_pending": 0, "matched_missing": 0, "downloads_sent": 0, "notifications_sent": 0, "errors": []}
        parsed = {"title": title, "is_hs": True, "hs_number": 2020}
        with mock.patch("blueprints.bedetheque.auto_acquire.queue_manual_review") as review:
            handler._process_typed_monitor_file(
                "ebdz", {"filename": filename, "link": "ed2k://x", "thread_url": "https://ebdz/t/19328"}, filename, parsed,
                handler._typed_identity(parsed), (499, 1, "/BD", "Mortelle Adèle"), 499, None, "", {}, "download",
                downloader, stats, mock.Mock())
        return downloader, review, stats

    def test_monitored_special_album_is_downloaded(self):
        downloader, review, stats = self.run_typed(FILE)
        downloader.send_torrent_download.assert_called_once()
        self.assertEqual((stats["downloads_sent"], stats["queued_for_review"]), (1, 0))
        review.assert_not_called()

    def test_ambiguous_special_album_goes_to_validation(self):
        downloader, review, stats = self.run_typed("Mortelle Adèle HS2020.cbz")
        downloader.send_torrent_download.assert_not_called()
        self.assertEqual(stats["queued_for_review"], 1)

    def test_already_downloading_is_not_sent_twice(self):
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO active_downloads (series_id, status, is_hs, hs_number) VALUES (499, \x27pending\x27, 1, 2020)")
        conn.commit()
        conn.close()
        downloader, _, stats = self.run_typed(FILE)
        downloader.send_torrent_download.assert_not_called()
        self.assertEqual(stats["skipped_pending"], 1)


if __name__ == "__main__":
    unittest.main()
