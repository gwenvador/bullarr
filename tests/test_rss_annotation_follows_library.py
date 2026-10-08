import os
import sqlite3
import tempfile
import unittest

from flask import Flask

from blueprints.ebdz import routes


class RssAnnotationFingerprintTest(unittest.TestCase):
    """Une release annotée avant l ajout de sa série doit être recalculée ensuite (cas Paci)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "bullarr.db")
        conn = sqlite3.connect(self.db)
        conn.executescript(
            "CREATE TABLE series (id INTEGER PRIMARY KEY, title TEXT);"
            "CREATE TABLE volumes (id INTEGER PRIMARY KEY, series_id INTEGER, filepath TEXT);"
            "CREATE TABLE active_downloads (id INTEGER PRIMARY KEY, status TEXT);")
        conn.commit()
        conn.close()
        app = Flask(__name__)
        app.config["DATABASE"] = self.db
        ctx = app.app_context()
        ctx.push()
        self.addCleanup(ctx.pop)

    def run_sql(self, sql):
        conn = sqlite3.connect(self.db)
        conn.execute(sql)
        conn.commit()
        conn.close()

    def test_fingerprint_is_stable_then_changes_with_series_volume_and_download_state(self):
        base = routes._rss_library_fingerprint()
        self.assertEqual(base, routes._rss_library_fingerprint())
        self.run_sql("INSERT INTO series (title) VALUES (\"Paci\")")
        after_series = routes._rss_library_fingerprint()
        self.assertNotEqual(base, after_series)
        self.run_sql("INSERT INTO volumes (series_id, filepath) VALUES (1, \"/BD/Paci/1.cbz\")")
        after_volume = routes._rss_library_fingerprint()
        self.assertNotEqual(after_series, after_volume)
        self.run_sql("INSERT INTO active_downloads (status) VALUES (\"pending\")")
        after_download = routes._rss_library_fingerprint()
        self.assertNotEqual(after_volume, after_download)
        self.run_sql("UPDATE active_downloads SET status = \"imported\"")
        self.assertNotEqual(after_download, routes._rss_library_fingerprint())

    def test_volume_without_file_does_not_change_the_fingerprint(self):
        base = routes._rss_library_fingerprint()
        self.run_sql("INSERT INTO volumes (series_id, filepath) VALUES (1, NULL)")
        self.assertEqual(base, routes._rss_library_fingerprint())


if __name__ == "__main__":
    unittest.main()
