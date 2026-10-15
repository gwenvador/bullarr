import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask

from blueprints.ebdz import routes as ebdz_routes
from blueprints.library.routes import get_owned_volume_signatures, volume_possession_status


class CompleteSeriesIntegralOwnedTest(unittest.TestCase):
    """Kenya: T1-T5 owned, the catalogue also lists an Intégrale (placeholder without
    file). A release of that intégrale is not missing anything."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
            CREATE TABLE series (id INTEGER PRIMARY KEY, library_id INTEGER, title TEXT, path TEXT,
                bedetheque_url TEXT, is_oneshot INTEGER DEFAULT 0, total_volumes INTEGER,
                bedetheque_total_volumes INTEGER);
            CREATE TABLE volumes (id INTEGER PRIMARY KEY, series_id INTEGER, volume_number INTEGER,
                filename TEXT, filepath TEXT, is_integral INTEGER DEFAULT 0, integral_number INTEGER,
                is_hs INTEGER DEFAULT 0, hs_number INTEGER, is_episode INTEGER DEFAULT 0,
                episode_number INTEGER, is_special INTEGER DEFAULT 0);
            CREATE TABLE missing_volume_monitor (series_id INTEGER, enabled INTEGER);
            CREATE TABLE missing_volume_downloads (title TEXT, source_link TEXT, success INTEGER);
            CREATE TABLE active_downloads (id INTEGER PRIMARY KEY, title TEXT, series_id INTEGER, volume_number INTEGER,
                is_integral INTEGER, integral_number INTEGER, is_hs INTEGER, hs_number INTEGER,
                is_episode INTEGER, episode_number INTEGER, status TEXT);
        """)
        conn.execute("INSERT INTO series (id, library_id, title, path, total_volumes) VALUES (476, 1, 'Kenya', '/lib/Kenya', 5)")
        conn.execute("INSERT INTO series (id, library_id, title, path, total_volumes) VALUES (500, 1, 'Partielle', '/lib/Partielle', 2)")
        conn.execute("INSERT INTO volumes (series_id, is_integral, filename) VALUES (476, 1, NULL)")
        for number in range(1, 6):
            conn.execute("INSERT INTO volumes (series_id, volume_number, filename, filepath) VALUES (476, ?, ?, ?)",
                         (number, f'Kenya {number}.cbz', f'/lib/Kenya/{number}.cbz'))
        conn.execute("INSERT INTO volumes (series_id, is_integral, integral_number) VALUES (500, 1, 1)")
        conn.execute("INSERT INTO volumes (series_id, volume_number, filename, filepath) VALUES (500, 1, 'P1.cbz', '/lib/Partielle/1.cbz')")
        conn.execute("INSERT INTO volumes (series_id, volume_number) VALUES (500, 2)")
        conn.commit()
        conn.close()
        self.app = Flask(__name__)
        self.app.config['DATABASE'] = self.db_path

    def tearDown(self):
        os.remove(self.db_path)

    def test_numbered_integral_of_complete_series_is_owned(self):
        conn = sqlite3.connect(self.db_path)
        owned = get_owned_volume_signatures([476, 500], conn=conn)
        conn.close()
        self.assertTrue(volume_possession_status(owned[476], is_integral=True, integral_number=1))
        self.assertFalse(volume_possession_status(owned[500], is_integral=True, integral_number=1))

    def test_rss_unnumbered_integral_of_complete_series_is_owned(self):
        events = [
            {'title': 'Kenya.Cycle.Integrale.Rodolphe.Leo.2023.FR.[CBZ]-TONER', 'link': 'https://example.invalid/1'},
            {'title': 'Partielle.Integrale.2023.FR.[CBZ]', 'link': 'https://example.invalid/2'},
        ]
        with self.app.app_context(), \
             patch.object(ebdz_routes, '_load_rss_match_overrides', return_value={}), \
             patch('blueprints.ebdz.routes.search_catalog_index', return_value=[], create=True):
            annotated = ebdz_routes._annotate_rss_entries(events)
        by_link = {entry['link']: entry for entry in annotated}
        self.assertEqual(by_link['https://example.invalid/1']['series_id'], 476)
        self.assertIs(by_link['https://example.invalid/1']['already_owned'], True)
        self.assertEqual(by_link['https://example.invalid/2']['series_id'], 500)
        self.assertIs(by_link['https://example.invalid/2']['already_owned'], False)


if __name__ == '__main__':
    unittest.main()
