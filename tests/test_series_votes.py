import re
import sqlite3
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SeriesVotesTest(unittest.TestCase):
    """total_votes : somme des votes Bédéthèque de tous les tomes d'une série (tri par popularité)."""

    def _subquery(self):
        source = (ROOT / 'blueprints/library/routes.py').read_text()
        match = re.search(r"\(SELECT COALESCE\(SUM\(CASE WHEN json_valid.*?\) AS total_votes", source, re.S)
        self.assertIsNotNone(match, 'sous-requête total_votes introuvable dans la liste de la bibliothèque')
        return match.group(0)

    def test_sums_votes_of_every_volume_and_ignores_bad_json(self):
        conn = sqlite3.connect(':memory:')
        conn.executescript('''
            CREATE TABLE series (id INTEGER PRIMARY KEY, title TEXT);
            CREATE TABLE volumes (id INTEGER PRIMARY KEY, series_id INTEGER, comicinfo TEXT);
            INSERT INTO series VALUES (1, 'Populaire'), (2, 'Sans vote'), (3, 'JSON cassé');
            INSERT INTO volumes (series_id, comicinfo) VALUES
                (1, '{"communityratingcount": 120}'), (1, '{"communityratingcount": "30"}'),
                (1, '{"title": "sans votes"}'), (1, NULL),
                (2, '{"title": "x"}'),
                (3, 'pas du json'), (3, '{"communityratingcount": 5}');
        ''')
        rows = dict(conn.execute(f'SELECT s.id, {self._subquery()} FROM series s'))
        self.assertEqual(rows, {1: 150, 2: 0, 3: 5})

    def test_library_table_has_a_sortable_votes_column(self):
        script = (ROOT / 'static/js/library.js').read_text()
        self.assertIn("key: 'votes'", script)
        self.assertIn("case 'votes': return s.total_votes || null;", script)


class LibraryGenreFilterTest(unittest.TestCase):
    """Colonne Genre du tableau de la bibliothèque : liste déroulante, un genre par option."""

    def test_genre_is_a_multi_value_dropdown_while_title_author_publisher_stay_text(self):
        script = (ROOT / 'static/js/library.js').read_text()
        labels = re.search(r"const _TEXT_FILTER_TABLE_COLUMN_LABELS = new Set\(\[(.*?)\]\)", script).group(1)
        self.assertNotIn("'Genre'", labels)
        for label in ("'Titre'", "'Auteur'", "'Éditeur'"):
            self.assertIn(label, labels)
        self.assertIn("genre: { label: 'Genre', type: 'select', getValue: s => s.local_genre || '—', multi: true }", script)


if __name__ == '__main__':
    unittest.main()
