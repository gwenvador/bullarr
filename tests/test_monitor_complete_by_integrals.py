import sqlite3
import unittest

from blueprints.missing_monitor import new_file_handler as handler


class CompleteByIntegralsTest(unittest.TestCase):
    """Trent : 3 intégrales possédées couvrent les 8 tomes parus, fiches T01-T08 sans fichier."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("""CREATE TABLE series (id INTEGER PRIMARY KEY, manual_complete_override INTEGER,
            bedetheque_complete INTEGER, bedetheque_complete_reason TEXT, bedetheque_total_volumes INTEGER)""")
        self.conn.executemany("INSERT INTO series VALUES (?, ?, ?, ?, ?)", [
            (1, 0, 1, "toutes les intégrales possédées (INT1, INT2, INT3), couvrant les 8 tomes parus", 8),
            (2, 1, 0, "", 5),
            (3, 0, 0, "tomes manquants : 2", 4),
            (4, 0, 1, "4/4 albums parus", 4),
        ])
        self.cursor = self.conn.cursor()

    def tearDown(self):
        self.conn.close()

    def test_integrals_cover_published_tomes_only(self):
        missing = {1: {1, 3, 8, 9}}
        handler._drop_volumes_covered_by_completion(self.cursor, [1], missing)
        self.assertEqual(missing[1], {9})  # T09 paru après : toujours surveillé

    def test_manual_complete_clears_everything(self):
        missing = {2: {1, 2, 6}}
        handler._drop_volumes_covered_by_completion(self.cursor, [2], missing)
        self.assertEqual(missing[2], set())

    def test_incomplete_or_album_complete_series_untouched(self):
        missing = {3: {2}, 4: {5}}
        handler._drop_volumes_covered_by_completion(self.cursor, [3, 4], missing)
        self.assertEqual(missing, {3: {2}, 4: {5}})


if __name__ == "__main__":
    unittest.main()
