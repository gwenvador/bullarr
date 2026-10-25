import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask

from blueprints.ebdz import routes as ebdz_routes
from series_title_match import parenthetical_prefix_title


class ParentheticalSeriesTitleTest(unittest.TestCase):
    """« Alix (Les Voyages d') » possédé : la release « Les Voyages d'Alix T26 » doit retrouver sa série
    sans la confondre avec la série « Alix »."""

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
        conn.execute("INSERT INTO series (id, library_id, title, path, total_volumes) VALUES (1025, 1, 'Alix', '/lib/Alix', 48)")
        conn.execute("INSERT INTO series (id, library_id, title, path, total_volumes) VALUES (1364, 1, \"Alix (Les Voyages d')\", '/lib/Voyages', 40)")
        conn.execute("INSERT INTO volumes (series_id, volume_number, filename, filepath) VALUES (1025, 1, 'a1.cbz', '/lib/Alix/1.cbz')")
        conn.execute("INSERT INTO volumes (series_id, volume_number, filename, filepath) VALUES (1364, 26, 'v26.cbz', '/lib/Voyages/26.cbz')")
        conn.commit()
        conn.close()
        self.app = Flask(__name__)
        self.app.config['DATABASE'] = self.db_path

    def tearDown(self):
        os.remove(self.db_path)

    def _annotate(self, titles):
        events = [{'title': title, 'link': f'https://example.invalid/{index}'} for index, title in enumerate(titles)]
        with self.app.app_context(), \
             patch.object(ebdz_routes, '_load_rss_match_overrides', return_value={}), \
             patch('blueprints.ebdz.routes.search_catalog_index', return_value=[], create=True):
            return ebdz_routes._annotate_rss_entries(events)

    def test_release_with_the_natural_title_finds_the_series_and_its_owned_volume(self):
        annotated = self._annotate([
            "Les.Voyages.D'Alix.T26.Les.Etrusques.2.Martin.Denoel.2007.FR.[PDF]-NOTAG",
            "Les.Voyages.D'Alix.T27.Les.Etrusques.3.2008.FR.[PDF]-NOTAG",
            'Alix.T01.Le.Sphinx.d.Or.1956.FR.[PDF]-NOTAG',
        ])
        voyage_owned, voyage_missing, plain = annotated
        self.assertEqual(voyage_owned['series_id'], 1364)
        self.assertIs(voyage_owned['already_owned'], True)
        self.assertEqual(voyage_missing['series_id'], 1364)
        self.assertIs(voyage_missing['already_owned'], False)
        # « Alix » reste la série simple, même si « Alix (Les Voyages d') » se réduit au même mot.
        self.assertEqual(plain['series_id'], 1025)
        self.assertIs(plain['already_owned'], True)

    def test_prefix_form_of_a_parenthetical_title(self):
        self.assertEqual(parenthetical_prefix_title("Alix (Les Voyages d')"), "Les Voyages d'Alix")
        self.assertEqual(parenthetical_prefix_title('Spirou (Le Petit)'), 'Le Petit Spirou')
        self.assertEqual(parenthetical_prefix_title('Kenya'), '')


class SharedSeriesTitleIndexTest(unittest.TestCase):
    """Telegram, l'import automatique et EBDZ identifient la série comme le RSS."""

    ROWS = [
        (1025, 1, '/lib', 'Alix', 0, None),
        (1364, 1, '/lib', "Alix (Les Voyages d')", 0, None),
        (1200, 1, '/lib', 'Spirou et Fantasio', 0, None),
    ]

    def test_auto_import_and_telegram_matcher_tell_the_two_alix_series_apart(self):
        from blueprints.library.routes import (
            _build_auto_import_series_match_index, _match_series_for_auto_import, _normalize_title_for_match)
        app = Flask(__name__)
        with app.app_context():
            index = _build_auto_import_series_match_index(self.ROWS)
            voyage = _match_series_for_auto_import(_normalize_title_for_match("Les Voyages D'Alix"), self.ROWS, index)
            plain = _match_series_for_auto_import(_normalize_title_for_match('Alix'), self.ROWS, index)
        self.assertEqual(voyage[0], 1364)
        self.assertEqual(plain[0], 1025)

    def test_index_keeps_the_short_key_for_the_plain_title_and_stays_ambiguous_between_aliased_ones(self):
        from series_title_match import index_series_by_title
        rows = [(1, 'Alix'), (2, "Alix (Les Voyages d')"), (3, 'Alix (Le Fils de)'), (4, 'Blake (Les Aventures de)')]
        index = index_series_by_title(rows, lambda row: row[1], lambda title: title.lower().replace("'", ' ').strip())
        self.assertEqual([row[0] for row in index['alix']], [1])
        self.assertEqual([row[0] for row in index["les voyages d alix"]], [2])
        self.assertEqual([row[0] for row in index['le fils de alix']], [3])
        # la clé du titre complet reste indexée quand aucune série simple ne la réclame
        self.assertEqual([row[0] for row in index['blake (les aventures de)']], [4])


if __name__ == '__main__':
    unittest.main()
