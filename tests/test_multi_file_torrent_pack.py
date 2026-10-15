import sqlite3
import tempfile
import unittest
from unittest import mock

from flask import Flask

from blueprints.missing_monitor import downloader


class MultiFileTorrentPackTest(unittest.TestCase):
    """Un torrent qBittorrent à plusieurs fichiers devient un pack dès qu\x27il est terminé."""

    def setUp(self):
        self.db = tempfile.NamedTemporaryFile(suffix=".db")
        self.app = Flask(__name__)
        self.app.config["DATABASE"] = self.db.name
        self.ctx = self.app.app_context()
        self.ctx.push()
        conn = sqlite3.connect(self.db.name)
        conn.execute("CREATE TABLE active_downloads (id INTEGER PRIMARY KEY, status TEXT, is_pack INTEGER, "
                     "client TEXT, client_item_id TEXT, volume_number INTEGER, completed_at TEXT)")
        conn.execute("INSERT INTO active_downloads VALUES (1, \x27pending\x27, 0, \x27qbittorrent\x27, \x27abc\x27, NULL, NULL)")
        conn.execute("INSERT INTO active_downloads VALUES (2, \x27pending\x27, 0, \x27qbittorrent\x27, \x27def\x27, 7, NULL)")
        conn.execute("INSERT INTO active_downloads VALUES (3, \x27pending\x27, 0, \x27amule\x27, \x27ghi\x27, NULL, NULL)")
        conn.commit()
        conn.close()

    def tearDown(self):
        self.ctx.pop()
        self.db.close()

    def state(self, download_id):
        conn = sqlite3.connect(self.db.name)
        row = conn.execute("SELECT status, is_pack FROM active_downloads WHERE id = ?", (download_id,)).fetchone()
        conn.close()
        return row

    def test_multi_file_torrent_is_promoted_to_pack(self):
        with mock.patch("blueprints.qbittorrent.routes.get_qbittorrent_torrent_file_count", return_value=17):
            downloader.mark_download_completed(1)
        self.assertEqual(self.state(1), ("completed", 1))

    def test_single_file_torrent_stays_an_album(self):
        with mock.patch("blueprints.qbittorrent.routes.get_qbittorrent_torrent_file_count", return_value=1):
            downloader.mark_download_completed(1)
        self.assertEqual(self.state(1), ("completed", 0))

    def test_download_tied_to_a_volume_or_other_client_is_not_asked(self):
        with mock.patch("blueprints.qbittorrent.routes.get_qbittorrent_torrent_file_count", return_value=17) as count:
            downloader.mark_download_completed(2)
            downloader.mark_download_completed(3)
        count.assert_not_called()
        self.assertEqual(self.state(2), ("completed", 0))
        self.assertEqual(self.state(3), ("completed", 0))

    def test_unreachable_client_keeps_the_state(self):
        with mock.patch("blueprints.qbittorrent.routes.get_qbittorrent_torrent_file_count", return_value=None):
            downloader.mark_download_completed(1)
        self.assertEqual(self.state(1), ("completed", 0))
