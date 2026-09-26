import sqlite3
import tempfile
import unittest
from pathlib import Path
from flask import Flask

from blueprints.library import import_history
from blueprints.library import routes


class ImportWaitingFileCountTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile()
        self.app = Flask(__name__)
        self.app.config['DATABASE'] = self.tmp.name
        with self.app.app_context():
            import_history.init_import_history_table()
            conn = sqlite3.connect(self.tmp.name)
            conn.execute('CREATE TABLE active_downloads (id INTEGER PRIMARY KEY, status TEXT, is_pack INTEGER)')
            conn.execute('CREATE TABLE series (id INTEGER PRIMARY KEY, is_oneshot INTEGER DEFAULT 0)')
            conn.executemany('INSERT INTO active_downloads VALUES (?, ?, ?)', [
                (1, 'completed', 0), (2, 'completed', 0), (3, 'completed', 1),
                (4, 'importing', 0), (5, 'completed', 0),
            ])
            conn.executemany('INSERT INTO import_items (item_key, tracking_id, source_path, filename, source_available, state, is_auxiliary, is_directory, is_pack_parent) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', [
                ('1:/imports/ready.cbz', 1, '/imports/ready.cbz', 'ready.cbz', 1, 'waiting', 0, 0, 0),
                ('2:/imports/failed.cbz', 2, '/imports/failed.cbz', 'failed.cbz', 1, 'waiting', 0, 0, 0),
                ('3:/imports/pack.cbz', 3, '/imports/pack.cbz', 'pack.cbz', 1, 'waiting', 0, 0, 1),
                ('4:/imports/active.cbz', 4, '/imports/active.cbz', 'active.cbz', 1, 'waiting', 0, 0, 0),
                ('5:/imports/missing.cbz', 5, '/imports/missing.cbz', 'missing.cbz', 0, 'waiting', 0, 0, 0),
                ('6:/imports/page.jpg', 6, '/imports/page.jpg', 'page.jpg', 1, 'waiting', 1, 0, 0),
            ])
            conn.execute('INSERT INTO import_history_files (operation_id, filename, source_path, action, status) VALUES (?, ?, ?, ?, ?)', ('op', 'done.cbz', '/imports/done.cbz', 'import', 'success'))
            conn.execute("INSERT INTO import_items (item_key, tracking_id, source_path, filename, source_available, state) VALUES (?, ?, ?, ?, 1, 'waiting')", ('7:/imports/done.cbz', 7, '/imports/done.cbz', 'done.cbz'))
            conn.commit(); conn.close()

    def tearDown(self):
        self.tmp.close()

    def test_shared_predicate_counts_ready_and_failed_files_only(self):
        with self.app.app_context():
            snapshot = import_history.get_waiting_import_snapshot()
        self.assertEqual(snapshot['waiting_file_count'], 2)
        self.assertEqual({row['filename'] for row in snapshot['items']}, {'ready.cbz', 'failed.cbz'})
        self.assertEqual(snapshot['awaiting_discovery_count'], 0)

    def test_unknown_pack_is_separate_and_force_replace_survives_old_history(self):
        with self.app.app_context():
            conn = sqlite3.connect(self.tmp.name)
            conn.execute('INSERT INTO active_downloads VALUES (?, ?, ?)', (9, 'completed', 1))
            conn.execute("INSERT INTO import_items (item_key, tracking_id, source_path, filename, source_available, state, force_replace) VALUES (?, ?, ?, ?, 1, 'waiting', 1)", ('8:/imports/replacement.cbz', 8, '/imports/replacement.cbz', 'replacement.cbz'))
            conn.execute('INSERT INTO import_history_files (operation_id, filename, source_path, action, status) VALUES (?, ?, ?, ?, ?)', ('op2', 'replacement.cbz', '/imports/replacement.cbz', 'import', 'success'))
            conn.commit(); conn.close()
            snapshot = import_history.get_waiting_import_snapshot()
        self.assertEqual(snapshot['waiting_file_count'], 3)
        self.assertEqual(snapshot['awaiting_discovery_count'], 1)
        self.assertIn('replacement.cbz', {row['filename'] for row in snapshot['items']})

    def test_badge_route_is_read_only_and_uses_exact_field(self):
        with self.app.app_context():
            with self.app.test_request_context('/api/import/pending-count'):
                response = routes.import_pending_count()
                payload = response.get_json()
        self.assertEqual(payload['waiting_file_count'], 2)
        self.assertNotIn('count', payload)
        source = Path('blueprints/library/routes.py').read_text()
        route = source[source.index('    from .import_history import get_waiting_import_snapshot'):source.index('def _extract_one_archive')]
        self.assertNotIn('get_pending_downloads', route)
        self.assertNotIn('_scan_tracked_import_files', route)

    def test_js_badge_keeps_exact_counts_and_refreshes_from_confirmed_state(self):
        source = Path('static/js/nav.js').read_text()
        start = source.index('function initImportBadge')
        end = source.index('// ===== Badge de nouveautés', start)
        source = source[start:end]
        self.assertIn('String(count)', source)
        self.assertNotIn("count > 99 ? '99+'", source)
        self.assertIn('lastConfirmedCount', source)
        self.assertIn('import-state-confirmed', source)


if __name__ == '__main__':
    unittest.main()
