"""Confirming an imported file must preserve the list of albums still missing."""
import json
import sqlite3
import unittest

from blueprints.library.routes import _mark_imported_volumes_present


class ImportMissingVolumesTests(unittest.TestCase):
    def test_import_confirmation_does_not_erase_unowned_placeholders(self):
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        conn.execute('''CREATE TABLE series (
            id INTEGER PRIMARY KEY, missing_volumes TEXT, filesystem_state TEXT,
            filesystem_missing_count INTEGER, filesystem_checked_at TEXT)''')
        conn.execute('''CREATE TABLE volumes (
            id INTEGER PRIMARY KEY, series_id INTEGER, volume_number INTEGER,
            filepath TEXT, filesystem_present INTEGER, filesystem_checked_at TEXT)''')
        conn.execute('INSERT INTO series (id, missing_volumes) VALUES (1, ?)', (json.dumps([2, 5, 6]),))
        conn.execute("INSERT INTO volumes VALUES (10, 1, 1, '/library/example/T1.cbz', 0, NULL)")
        for number in (2, 5, 6):
            conn.execute('INSERT INTO volumes (series_id, volume_number) VALUES (1, ?)', (number,))
        _mark_imported_volumes_present(conn, [10])
        row = conn.execute('SELECT missing_volumes, filesystem_state FROM series WHERE id=1').fetchone()
        self.assertEqual(json.loads(row[0]), [2, 5, 6])
        self.assertEqual(row[1], 'present')


if __name__ == '__main__':
    unittest.main()
