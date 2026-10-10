import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

from flask import Flask

from blueprints.library import routes


class MultiFileDownloadTest(unittest.TestCase):
    """Cas « Le Gang de la clef à molette »: un torrent qui livre le même album en CBZ et en PDF.
    Le premier fichier importé ne doit pas clore le téléchargement et abandonner l autre."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.db = tempfile.NamedTemporaryFile()
        self.app = Flask(__name__)
        self.app.config["DATABASE"] = self.db.name
        self.app.config["IMPORT_DIRECTORIES"] = [self.root]
        conn = sqlite3.connect(self.db.name)
        conn.execute("CREATE TABLE active_downloads (id INTEGER PRIMARY KEY, status TEXT, is_pack INTEGER, completed_at TEXT, expected_volume_count INTEGER)")
        conn.execute("INSERT INTO active_downloads VALUES (1, \x27importing\x27, 0, NULL, NULL)")
        conn.commit()
        conn.close()
        self.folder = os.path.join(self.root, "Le Gang de la clef a molette")
        os.mkdir(self.folder)
        self.pdf = os.path.join(self.folder, "Le Gang de la clef a molette.pdf")
        self.cbz = os.path.join(self.folder, "Le Gang de la clef a molette.cbz")
        for path in (self.pdf, self.cbz):
            open(path, "w").close()
        self.finalized = set()
        patches = [
            mock.patch("blueprints.library.import_history.get_finalized_import_source_paths", side_effect=lambda: set(self.finalized)),
            mock.patch("blueprints.missing_monitor.downloader.download_still_incomplete", return_value=False),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        routes._deferred_pack_finalizations.clear()
        routes._pack_transfer_activity.clear()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.addCleanup(self.ctx.pop)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.addCleanup(self.db.close)

    def row(self):
        conn = sqlite3.connect(self.db.name)
        try:
            return conn.execute("SELECT status, is_pack FROM active_downloads WHERE id = 1").fetchone()
        finally:
            conn.close()

    def test_first_imported_file_keeps_the_download_open_as_a_pack(self):
        self.finalized.add(self.pdf)
        routes._maybe_complete_tracking_after_move(self.pdf, {"tracking_id": 1}, source_was_copied=True)
        self.assertEqual(self.row(), ("completed", 1))

    def test_moved_source_with_a_remaining_sibling_also_stays_open(self):
        os.remove(self.pdf)
        routes._maybe_complete_tracking_after_move(self.pdf, {"tracking_id": 1})
        self.assertEqual(self.row(), ("completed", 1))

    def test_a_single_file_download_is_still_closed_right_away(self):
        os.remove(self.cbz)
        self.finalized.add(self.pdf)
        routes._maybe_complete_tracking_after_move(self.pdf, {"tracking_id": 1}, source_was_copied=True)
        self.assertEqual(self.row(), ("imported", 0))

    def test_download_top_level_dir(self):
        self.assertEqual(routes._download_top_level_dir(self.pdf), os.path.realpath(self.folder))
        self.assertIsNone(routes._download_top_level_dir(os.path.join(self.root, "loose.cbz")))
        self.assertIsNone(routes._download_top_level_dir("/elsewhere/x/y.cbz"))


class FormatOrderTest(unittest.TestCase):
    def test_best_format_is_imported_first(self):
        captured = {}

        def fake_batch(files, **kwargs):
            captured["order"] = [f["filename"] for f in files]
            return True, "op", {"imported_count": 0, "replaced_count": 0, "skipped_count": 0, "failed_count": 0, "failures": []}

        files = [{"filename": "A.pdf"}, {"filename": "A.cbr"}, {"filename": "B.cbz"}, {"filename": "A.cbz"}]
        with mock.patch.object(routes, "_execute_import_batch", side_effect=fake_batch):
            routes.execute_auto_import(files)
        self.assertEqual(captured["order"], ["B.cbz", "A.cbz", "A.cbr", "A.pdf"])


if __name__ == "__main__":
    unittest.main()
