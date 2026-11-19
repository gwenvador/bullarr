import sqlite3
import unittest

from blueprints.komga.user_link import SERVER_SOURCE


class NearlyFinishedCountsAsReadTest(unittest.TestCase):
    """« pour les BD lues à plus de 98 %, il reste 2 ou 3 pages max : considère-les comme lues »."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE volumes (id INTEGER PRIMARY KEY, komga_book_id TEXT, page_count INTEGER)")
        self.conn.execute("CREATE TABLE komga_read_progress (komga_book_id TEXT, page INTEGER, pages_count INTEGER, "
                          "completed INTEGER, read_at TEXT, synced_at TEXT)")

    def completed(self, page, pages, done=0, volume_pages=None):
        self.conn.execute("DELETE FROM volumes")
        self.conn.execute("DELETE FROM komga_read_progress")
        self.conn.execute("INSERT INTO volumes VALUES (1, \x27b\x27, ?)", (volume_pages,))
        self.conn.execute("INSERT INTO komga_read_progress VALUES (\x27b\x27, ?, ?, ?, NULL, NULL)", (page, pages, done))
        return self.conn.execute(f"SELECT completed FROM {SERVER_SOURCE.sql} p WHERE p.vol_id = 1").fetchone()[0]

    def test_last_pages_count_as_read(self):
        self.assertEqual(self.completed(65, 67), 1)
        self.assertEqual(self.completed(64, 67), 1)
        self.assertEqual(self.completed(99, 100), 1)

    def test_page_count_of_the_volume_is_used_when_progress_has_none(self):
        self.assertEqual(self.completed(46, None, volume_pages=48), 1)

    def test_earlier_pages_stay_in_progress(self):
        self.assertEqual(self.completed(60, 67), 0)
        self.assertEqual(self.completed(8, 10), 0)
        self.assertEqual(self.completed(0, 67), 0)

    def test_completed_flag_is_kept(self):
        self.assertEqual(self.completed(1, 67, done=1), 1)


if __name__ == "__main__":
    unittest.main()
