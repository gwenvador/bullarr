import json
import os
import sqlite3
import tempfile
import unittest

from config import Config


class PackAlbumTitleTest(unittest.TestCase):
    """Pack « Le reste du monde » : fichiers « <titre de l'album> - <auteur>.cbz » sans numéro,
    reconnus par le titre d'album d'un tome de la série de destination."""

    def setUp(self):
        os.makedirs('data', exist_ok=True)
        fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        Config._init_database(self.db_path)
        from app import create_app
        self.app = create_app()
        self.app.config.update(DATABASE=self.db_path, TESTING=True)
        conn = sqlite3.connect(self.db_path)
        if "comicinfo" not in {row[1] for row in conn.execute("PRAGMA table_info(volumes)")}:
            conn.execute("ALTER TABLE volumes ADD COLUMN comicinfo TEXT")
        if "is_bis" not in {row[1] for row in conn.execute("PRAGMA table_info(volumes)")}:
            conn.execute("ALTER TABLE volumes ADD COLUMN is_bis INTEGER DEFAULT 0")
        conn.execute("INSERT INTO libraries (id, name, path) VALUES (1, 'BD', '/BD')")
        conn.execute("INSERT INTO series (id, library_id, title, path) VALUES (7, 1, 'Le reste du monde', '/BD/x')")
        for number, title in [(1, 'Le reste du monde'), (2, "Le monde d'après"), (3, 'Les frontières'),
                              (4, 'Les enfers'), (5, 'Les jours heureux')]:
            conn.execute("INSERT INTO volumes (series_id, volume_number, comicinfo) VALUES (7, ?, ?)",
                         (number, json.dumps({'title': title})))
        conn.commit()
        conn.close()
        self.destination = {'series_id': 7, 'series_title': 'Le reste du monde'}

    def tearDown(self):
        os.remove(self.db_path)

    def check(self, title):
        from blueprints.library.routes import _pack_file_matches_destination
        parsed = {'title': title, 'volume': None}
        with self.app.app_context():
            ok = _pack_file_matches_destination(parsed, self.destination)
        return ok, parsed.get('volume')

    def test_album_titles_give_the_volume(self):
        self.assertEqual(self.check("L'enfer - Chauzy Jean"), (True, 4))
        self.assertEqual(self.check("Le monde d'apres - Chauzy Jean"), (True, 2))
        self.assertEqual(self.check('Les Frontieres - Chauzy Jean'), (True, 3))
        self.assertEqual(self.check('Le reste du monde - Chauzy Jean'), (True, 1))

    def test_unrelated_title_is_still_refused(self):
        self.assertEqual(self.check('Blacksad - Canales'), (False, None))

    def test_conflicting_volume_is_refused(self):
        from blueprints.library.routes import _pack_file_matches_destination
        parsed = {'title': "L'enfer - Chauzy Jean", 'volume': 2}
        with self.app.app_context():
            self.assertFalse(_pack_file_matches_destination(parsed, self.destination))


if __name__ == '__main__':
    unittest.main()
