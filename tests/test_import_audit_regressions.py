import os
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from flask import Flask

from blueprints.library import import_history
from blueprints.qbittorrent import routes as qbittorrent_routes
from blueprints.activity import routes as activity_routes
from blueprints.missing_monitor import downloader


class ImportAuditRegressionsTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = os.path.join(self.tempdir.name, 'test.sqlite')
        self.app = Flask(__name__)
        self.app.config['DATABASE'] = self.database
        with self.app.app_context():
            import_history.init_import_history_table()

    def tearDown(self):
        self.tempdir.cleanup()

    def test_manual_override_is_not_removed_by_same_basename_elsewhere(self):
        first = os.path.join(self.tempdir.name, 'first')
        second = os.path.join(self.tempdir.name, 'second')
        os.mkdir(first)
        os.mkdir(second)
        imported = os.path.join(first, 'album.cbz')
        still_waiting = os.path.join(second, 'album.cbz')
        for path in (imported, still_waiting):
            with open(path, 'wb') as stream:
                stream.write(b'archive')
        conn = sqlite3.connect(self.database)
        conn.execute('CREATE TABLE import_manual_overrides (filepath TEXT PRIMARY KEY, destination_json TEXT)')
        conn.executemany('INSERT INTO import_manual_overrides (filepath) VALUES (?)', [(imported,), (still_waiting,)])
        conn.execute(
            "INSERT INTO import_history_files (operation_id, filename, source_path, action, status) "
            "VALUES ('op', 'album.cbz', ?, 'imported', 'success')",
            (imported,),
        )
        conn.commit()
        conn.close()

        with self.app.app_context():
            overrides = import_history.get_manual_override_filepaths()

        self.assertEqual(overrides, {still_waiting})

    def test_unreadable_root_keeps_its_database_item_available(self):
        first = os.path.join(self.tempdir.name, 'first')
        second = os.path.join(self.tempdir.name, 'second')
        os.mkdir(first)
        os.mkdir(second)
        first_file = os.path.join(first, 'a.cbz')
        second_file = os.path.join(second, 'b.cbz')
        items = [
            {'filepath': first_file, 'filename': 'a.cbz', 'destination': {'tracking_id': 1}},
            {'filepath': second_file, 'filename': 'b.cbz', 'destination': {'tracking_id': 2}},
        ]
        with self.app.app_context():
            import_history.persist_discovered_import_items(items, scanned_roots=[first, second])
            import_history.persist_discovered_import_items([], scanned_roots=[first])
        conn = sqlite3.connect(self.database)
        available = dict(conn.execute('SELECT source_path, source_available FROM import_items'))
        conn.close()
        self.assertEqual(available[first_file], 0)
        self.assertEqual(available[second_file], 1)

    def test_qbittorrent_name_is_cached_by_exact_client_hash(self):
        conn = sqlite3.connect(self.database)
        conn.execute(
            'CREATE TABLE active_downloads ('
            'id INTEGER PRIMARY KEY, client TEXT, client_item_id TEXT, client_item_name TEXT)'
        )
        conn.executemany(
            'INSERT INTO active_downloads VALUES (?, ?, ?, NULL)',
            [(1, 'qbittorrent', 'abc'), (2, 'qbittorrent', 'def')],
        )
        conn.commit()
        conn.close()
        response = Mock()
        response.json.return_value = [{'hash': 'ABC', 'name': 'Dossier réel'}]
        session = Mock()
        session.get.return_value = response
        with self.app.app_context(), \
                patch.object(qbittorrent_routes, 'load_qbittorrent_config', return_value={'enabled': True}), \
                patch.object(qbittorrent_routes, 'create_qbittorrent_session', return_value=(session, 'http://client', None)):
            names = qbittorrent_routes.get_qbittorrent_torrent_names({'abc'})
        conn = sqlite3.connect(self.database)
        stored = dict(conn.execute('SELECT id, client_item_name FROM active_downloads'))
        conn.close()
        self.assertEqual(names, {'abc': 'Dossier réel'})
        self.assertEqual(stored, {1: 'Dossier réel', 2: None})

    def test_completed_download_does_not_probe_client_during_page_read(self):
        completed = {
            'id': 1, 'client': 'qbittorrent', 'status': 'completed',
            'client_item_id': 'abc', 'title': 'Album.cbz',
        }
        with self.app.test_request_context('/api/activity/status'), \
                patch.object(downloader, 'get_pending_downloads', return_value=[completed]), \
                patch.object(activity_routes, '_qbittorrent_status', side_effect=AssertionError('client probed')):
            response = activity_routes.activity_status()
        self.assertEqual(response.get_json()['pending'], [completed])


if __name__ == '__main__':
    unittest.main()
