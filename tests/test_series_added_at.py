import sqlite3
import unittest
from pathlib import Path

from config import Config

ROOT = Path(__file__).resolve().parents[1]


class SeriesAddedAtTest(unittest.TestCase):
    def make_db(self):
        conn = sqlite3.connect(':memory:')
        conn.executescript("""
            CREATE TABLE series (id INTEGER PRIMARY KEY, title TEXT);
            CREATE TABLE action_history (id INTEGER PRIMARY KEY, action_type TEXT, series_id INTEGER, created_at TEXT);
            INSERT INTO series (id, title) VALUES (1, 'Avec ajout'), (2, 'Premiere action'), (3, 'Sans trace');
            INSERT INTO action_history (action_type, series_id, created_at) VALUES
                ('search', 1, '2026-01-05 10:00:00'), ('add_series', 1, '2026-01-02 09:00:00'),
                ('rename', 2, '2026-03-01 08:00:00'), ('search', 2, '2026-02-01 08:00:00');
        """)
        return conn

    def test_existing_series_get_their_first_known_date_and_new_ones_get_now(self):
        conn = self.make_db()
        Config._add_series_extra_columns(conn, ':memory:')
        dates = dict(conn.execute('SELECT id, added_at FROM series'))
        self.assertEqual(dates[1], '2026-01-02 09:00:00')   # l'ajout lui-même
        self.assertEqual(dates[2], '2026-02-01 08:00:00')   # sinon la première action
        self.assertIsNone(dates[3])                          # sans trace: pas de date inventée
        conn.execute("INSERT INTO series (id, title) VALUES (4, 'Nouvelle')")
        self.assertIsNotNone(conn.execute('SELECT added_at FROM series WHERE id = 4').fetchone()[0])
        Config._add_series_extra_columns(conn, ':memory:')   # idempotent
        self.assertEqual(dict(conn.execute('SELECT id, added_at FROM series'))[1], '2026-01-02 09:00:00')

    def test_table_has_a_sortable_added_date_column(self):
        script = (ROOT / 'static/js/library.js').read_text()
        self.assertIn("key: 'addedAt'", script)
        self.assertNotIn("key: 'scanDate'", script)  # colonne « Dernier scan » retirée à la demande
        self.assertIn("key: 'lastChange'", script)
        self.assertIn("case 'lastChange': return s.last_changed_at || null;", script)
        self.assertIn("case 'addedAt': return s.added_at || null;", script)
        api = (ROOT / 'blueprints/library/routes.py').read_text()
        self.assertIn("'added_at': row['added_at'],", api)


class LastChangeDatesTest(unittest.TestCase):
    def test_last_change_ignores_searches_and_skipped_files(self):
        from blueprints.library.routes import _series_last_change_dates
        conn = sqlite3.connect(':memory:')
        conn.executescript("""
            CREATE TABLE import_history_files (id INTEGER PRIMARY KEY, series_id INTEGER, action TEXT, status TEXT, created_at TEXT);
            CREATE TABLE action_history (id INTEGER PRIMARY KEY, series_id INTEGER, action_type TEXT, created_at TEXT);
            INSERT INTO import_history_files (series_id, action, status, created_at) VALUES
                (1, 'imported', 'success', '2026-10-01 10:00:00'), (1, 'skipped', 'success', '2026-10-09 10:00:00'),
                (1, 'imported', 'error', '2026-10-10 10:00:00'), (2, 'replaced', 'success', '2026-09-01 10:00:00');
            INSERT INTO action_history (series_id, action_type, created_at) VALUES
                (1, 'search', '2026-10-20 10:00:00'), (1, 'auto_acquire', '2026-10-21 10:00:00'),
                (1, 'rename', '2026-10-05 10:00:00'), (3, 'search', '2026-10-22 10:00:00');
        """)
        self.assertEqual(_series_last_change_dates(conn.cursor()),
                         {1: '2026-10-05 10:00:00', 2: '2026-09-01 10:00:00'})


if __name__ == '__main__':
    unittest.main()
