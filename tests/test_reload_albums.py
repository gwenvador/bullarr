"""« Recharger les albums » : rien n'est écrit sans nouvel album ; sinon seule la liste
des albums change et les nouveaux deviennent des tomes manquants."""
import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from flask import Flask

from blueprints.bedetheque import routes


OLD = [{'id': '1', 'number': 1, 'title': 'Un'}, {'id': '2', 'number': 2, 'title': 'Deux'}]


class ReloadAlbumsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, 'b.db')
        conn = sqlite3.connect(self.db)
        conn.execute('''CREATE TABLE series (id INTEGER PRIMARY KEY, title TEXT, bedetheque_url TEXT,
            bedetheque_albums TEXT, bedetheque_total_volumes INTEGER, bedetheque_description TEXT,
            bedetheque_updated_at TEXT)''')
        conn.execute("INSERT INTO series VALUES (5, 'Gaston', 'https://www.bedetheque.com/serie-1.html', ?, 2, 'résumé', 'avant')",
                     (json.dumps(OLD),))
        conn.commit()
        conn.close()
        self.app = Flask(__name__)
        self.app.config['DATABASE'] = self.db

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, volumes):
        info = {'volumes': volumes, 'description': 'nouveau résumé', 'title': 'Autre titre'}
        with self.app.app_context(), \
                mock.patch.object(routes.BedethequeScraper, 'get_series_info', return_value=info), \
                mock.patch.object(routes, '_sync_bedetheque_placeholder_volumes') as sync:
            payload, status = routes.reload_series_albums(5)
        row = sqlite3.connect(self.db).execute(
            'SELECT title, bedetheque_albums, bedetheque_total_volumes, bedetheque_description, bedetheque_updated_at FROM series').fetchone()
        return payload, status, row, sync

    def test_no_new_album_writes_nothing(self):
        payload, status, row, sync = self._run(OLD)
        self.assertEqual((status, payload['new_albums']), (200, []))
        self.assertEqual(row[4], 'avant')
        sync.assert_not_called()

    def test_new_album_is_added_and_nothing_else_changes(self):
        payload, status, row, sync = self._run(OLD + [{'id': '3', 'number': 3.0, 'title': 'Trois'}])
        self.assertEqual(payload['new_albums'], ['T3 - Trois'])
        self.assertEqual(len(json.loads(row[1])), 3)
        self.assertEqual((row[0], row[3]), ('Gaston', 'résumé'))
        self.assertNotEqual(row[4], 'avant')
        sync.assert_called_once()


if __name__ == '__main__':
    unittest.main()
