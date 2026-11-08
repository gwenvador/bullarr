"""Lecteur : tomes précédent / suivant et sens manga par défaut (GET /api/volumes/<id>/reader)."""
import os
import sqlite3
import tempfile
import unittest

from flask import Flask

from blueprints.library.scanner import LibraryScanner


class ReaderNeighboursTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, 'bullarr.db')
        from config import Config
        Config._init_database(self.db)
        LibraryScanner(self.db)
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO libraries (id, name, path) VALUES (1, 'BD', '/BD')")
        conn.execute("INSERT INTO series (id, library_id, title, path, bedetheque_origin) VALUES (1, 1, 'One Piece', '/BD/OP', 'Asie')")
        rows = [  # (id, numéro, intégrale, HS, fichier)
            (10, 2, 0, 0, '/BD/OP/T02.cbz'), (11, 1, 0, 0, '/BD/OP/T01.cbz'), (12, 3, 0, 0, None),
            (13, 4, 0, 0, '/BD/OP/T04.cbz'), (14, None, 0, 1, '/BD/OP/HS1.cbz'),
        ]
        for vid, number, integral, hs, path in rows:
            conn.execute('INSERT INTO volumes (id, series_id, volume_number, is_integral, is_hs, hs_number, filename, filepath) '
                         'VALUES (?, 1, ?, ?, ?, ?, ?, ?)', (vid, number, integral, hs, 1 if hs else None,
                                                           os.path.basename(path or 'x'), path))
        conn.commit()
        conn.close()
        self.app = Flask(__name__)
        self.app.config['DATABASE'] = self.db

    def neighbours(self, volume_id):
        from blueprints.library import routes
        with self.app.app_context():
            return routes._reader_neighbours(1, volume_id)

    def test_owned_volumes_in_series_order(self):
        previous, following, manga = self.neighbours(10)
        self.assertEqual(previous, {'volume_id': 11, 'label': 'Tome 1'})
        # Le tome 3 n'a pas de fichier : le suivant du tome 2 est le tome 4.
        self.assertEqual(following, {'volume_id': 13, 'label': 'Tome 4'})
        self.assertTrue(manga)

    def test_ends_of_the_series(self):
        self.assertIsNone(self.neighbours(11)[0])
        self.assertEqual(self.neighbours(13)[1], {'volume_id': 14, 'label': 'HS 1'})
        self.assertIsNone(self.neighbours(14)[1])


    def test_resume_page_comes_from_komga(self):
        from blueprints.library import routes
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE volumes SET komga_book_id = 'b10' WHERE id = 10")
        conn.execute("UPDATE volumes SET komga_book_id = 'b11' WHERE id = 11")
        conn.execute("INSERT INTO komga_read_progress (komga_book_id, page, pages_count, completed) VALUES ('b10', 17, 46, 0)")
        conn.execute("INSERT INTO komga_read_progress (komga_book_id, page, pages_count, completed) VALUES ('b11', 46, 46, 1)")
        conn.commit()
        conn.close()
        with self.app.app_context():
            self.assertEqual(routes._reader_komga_progress(10), (17, False))
            # Album terminé : pas de reprise, le lecteur repart du début.
            self.assertEqual(routes._reader_komga_progress(11), (None, True))
            self.assertEqual(routes._reader_komga_progress(999), (None, False))
            self.assertEqual(routes._reader_komga_progress(None), (None, False))


if __name__ == '__main__':
    unittest.main()
