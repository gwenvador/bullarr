import json
import sqlite3
import unittest

from blueprints.library.routes import _merge_cached_bedetheque_comicinfo


class ImportMetadataCompletionTest(unittest.TestCase):
    def test_placeholder_completion_restores_full_cached_album_metadata(self):
        conn = sqlite3.connect(':memory:')
        conn.execute("CREATE TABLE series (id INTEGER PRIMARY KEY, bedetheque_url TEXT, bedetheque_albums TEXT, bedetheque_description TEXT, bedetheque_genre TEXT, bedetheque_scenaristes TEXT, bedetheque_dessinateurs TEXT, bedetheque_editeurs TEXT)")
        conn.execute("INSERT INTO series VALUES (1, ?, ?, ?, ?, ?, ?, ?)", (
            'https://example.invalid/series',
            json.dumps([{
                'number': 4, 'title': 'Escarmouches', 'scenario': 'Patrick Cothias',
                'dessin': 'David Prudhomme', 'couleurs': 'David Prudhomme',
                'editeur': 'Glénat', 'url': 'https://example.invalid/tome-4',
                'date_publication': '1997-09-01', 'rating': 4.8, 'rating_count': 4,
            }]),
            None, 'Histoire', 'Patrick Cothias', 'David Prudhomme', 'Glénat'))
        conn.commit()
        result = _merge_cached_bedetheque_comicinfo(conn.cursor(), 1, 'Ninon secrète', 4, {'title': 'Escarmouches'})
        self.assertEqual(result['series'], 'Ninon secrète')
        self.assertEqual(result['number'], '4')
        self.assertEqual(result['colorist'], 'David Prudhomme')
        self.assertEqual(result['month'], '9')
        self.assertEqual(result['communityrating'], '4.8')
        conn.close()


if __name__ == '__main__':
    unittest.main()
