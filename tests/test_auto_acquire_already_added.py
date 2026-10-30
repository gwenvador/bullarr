import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from flask import Flask

from blueprints.bedetheque import auto_acquire
from blueprints.missing_monitor import downloader

RELEASE = 'Tomka.le.gitan.de.Guernica.Carlotto.Palumbo.FR.[JPG]-NOTAG'


class AlreadyDownloadedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, 'bullarr.db')
        conn = sqlite3.connect(self.db)
        conn.execute('CREATE TABLE active_downloads (id INTEGER PRIMARY KEY, title TEXT, client TEXT, status TEXT, series_id INTEGER, volume_number INTEGER)')
        conn.execute("INSERT INTO active_downloads (title, client, status, series_id) VALUES (?, 'qbittorrent', 'completed', 1405)", (RELEASE,))
        conn.execute("INSERT INTO active_downloads (title, client, status, series_id) VALUES ('Autre', 'qbittorrent', 'imported', 1405)")
        conn.commit()
        conn.close()
        self.app = Flask(__name__)
        self.app.config['DATABASE'] = self.db

    def test_finished_but_not_imported_download_counts_as_duplicate(self):
        with self.app.app_context():
            self.assertIsNotNone(downloader.find_pending_download_duplicate(RELEASE, 1405, None))
            self.assertIsNone(downloader.find_pending_download_duplicate('Autre', 1405, None))

    def test_qbittorrent_conflict_is_reported_as_already_added(self):
        sender = mock.Mock()
        sender.return_value.send_torrent_download.return_value = (False, 'Erreur qBittorrent: HTTP 409')
        result = {'source': 'prowlarr', 'filename': RELEASE, 'link': 'magnet:?xt=urn:btih:abc'}
        with mock.patch('blueprints.missing_monitor.downloader.find_pending_download_duplicate', return_value=None), \
                mock.patch('blueprints.missing_monitor.downloader.MissingVolumeDownloader', sender):
            success, message = auto_acquire._download_result(self.app, result, 1405, 'Tomka', None)
        self.assertFalse(success)
        self.assertTrue(message.startswith(auto_acquire.ALREADY_ADDED_PREFIX))
