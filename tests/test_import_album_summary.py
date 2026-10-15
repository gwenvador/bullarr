import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from blueprints.library import routes


class ImportAlbumSummaryTest(unittest.TestCase):
    def _db(self, path, rows):
        conn = sqlite3.connect(path)
        conn.execute('CREATE TABLE volumes (id INTEGER PRIMARY KEY, filepath TEXT, format TEXT, comicinfo TEXT)')
        conn.executemany('INSERT INTO volumes VALUES (?, ?, ?, ?)', rows)
        conn.commit()
        conn.close()

    def test_only_volumes_without_summary_are_fetched(self):
        with tempfile.NamedTemporaryFile() as db_file:
            self._db(db_file.name, [
                (1, '/lib/a.cbz', 'cbz', json.dumps({'web': 'https://www.bedetheque.com/BD-a-1.html'})),
                (2, '/lib/b.cbz', 'cbz', json.dumps({'web': 'https://www.bedetheque.com/BD-b-2.html', 'summary': 'Déjà là'})),
                (3, '/lib/c.cbz', 'cbz', json.dumps({'title': 'Sans lien'})),
            ])
            with patch('blueprints.bedetheque.scraper.BedethequeScraper') as scraper_cls, \
                 patch('blueprints.bedetheque.comicinfo_writer.apply_volume_comicinfo') as apply:
                scraper_cls.return_value.fetch_album_description.return_value = 'Résumé album'
                filled = routes._fill_missing_album_summaries(db_file.name, [1, 2, 3])
            self.assertEqual(filled, 1)
            scraper_cls.return_value.fetch_album_description.assert_called_once_with(
                'https://www.bedetheque.com/BD-a-1.html'
            )
            apply.assert_called_once_with(db_file.name, 1, '/lib/a.cbz', 'cbz', {'Summary': 'Résumé album'})

    def test_empty_album_page_writes_nothing(self):
        with tempfile.NamedTemporaryFile() as db_file:
            self._db(db_file.name, [
                (1, '/lib/a.cbz', 'cbz', json.dumps({'web': 'https://www.bedetheque.com/BD-a-1.html'})),
            ])
            with patch('blueprints.bedetheque.scraper.BedethequeScraper') as scraper_cls, \
                 patch('blueprints.bedetheque.comicinfo_writer.apply_volume_comicinfo') as apply:
                scraper_cls.return_value.fetch_album_description.return_value = ''
                self.assertEqual(routes._fill_missing_album_summaries(db_file.name, [1]), 0)
            apply.assert_not_called()


if __name__ == '__main__':
    unittest.main()
