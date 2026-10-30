import os, sqlite3, tempfile, unittest
from unittest import mock
from flask import Flask
from blueprints.missing_monitor import new_file_handler as handler


class IntegralOnCompleteSeriesTest(unittest.TestCase):
    """Complainte des Landes perdues: 16 tomes possédés, intégrales Bédéthèque sans fichier."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "bullarr.db")
        conn = sqlite3.connect(self.db)
        conn.executescript("""
            CREATE TABLE volumes (id INTEGER PRIMARY KEY, series_id INTEGER, volume_number INTEGER, is_bis INTEGER DEFAULT 0,
                is_integral INTEGER DEFAULT 0, integral_number INTEGER, is_hs INTEGER DEFAULT 0, hs_number INTEGER,
                is_episode INTEGER DEFAULT 0, episode_number INTEGER, is_special INTEGER DEFAULT 0, special_label TEXT,
                filepath TEXT, comicinfo TEXT);
            CREATE TABLE active_downloads (id INTEGER PRIMARY KEY, series_id INTEGER, status TEXT, is_hs INTEGER DEFAULT 0,
                hs_number INTEGER, is_integral INTEGER DEFAULT 0, integral_number INTEGER, is_episode INTEGER DEFAULT 0, episode_number INTEGER);
        """)
        conn.execute("INSERT INTO volumes (series_id, volume_number, filepath) VALUES (1, 1, '/BD/1.cbz')")
        conn.execute("INSERT INTO volumes (series_id, is_integral, integral_number) VALUES (1, 1, 2)")
        conn.execute("INSERT INTO volumes (series_id, volume_number, filepath) VALUES (2, 1, '/BD/1.cbz')")
        conn.execute("INSERT INTO volumes (series_id, volume_number) VALUES (2, 2)")
        conn.commit()
        conn.close()
        app = Flask(__name__)
        app.config["DATABASE"] = self.db
        self.ctx = app.app_context()
        self.ctx.push()
        self.addCleanup(self.ctx.pop)

    def run_item(self, series_id):
        stats = {"queued_for_review": 0, "matched_missing": 0, "skipped_pending": 0}
        typed = ("is_integral", "integral_number", 2, "Intégrale 2")
        parsed = {"title": "Complainte des Landes perdues Dufaux Rosinski Cycle 1"}
        match = (series_id, None, None, "Complainte des Landes perdues")
        with mock.patch("blueprints.bedetheque.auto_acquire.queue_manual_review") as queue:
            handler._process_typed_monitor_file(
                "ebdz", {"filename": "x.cbz"}, "x.cbz", parsed, typed, match, series_id, None, "", {}, "download",
                mock.Mock(), stats, mock.Mock())
        return stats, queue

    def test_complete_series_ignores_integral(self):
        stats, queue = self.run_item(1)
        queue.assert_not_called()
        self.assertEqual(stats["queued_for_review"], 0)

    def test_series_with_missing_tome_still_reviews_integral(self):
        # Un tome numéroté manque: l'intégrale reste utile, on garde le comportement existant.
        self.assertTrue(handler._has_missing_regular_volume(2))
        self.assertFalse(handler._has_missing_regular_volume(1))
