import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

from flask import Flask

from blueprints.library import import_history, routes
from blueprints.missing_monitor import downloader


class CompletedPackReconcileTest(unittest.TestCase):
    """Un pack 'completed' dont il ne reste que des fichiers déjà traités doit se clore."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.db = tempfile.NamedTemporaryFile(suffix='.db')
        self.app = Flask(__name__)
        self.app.config['DATABASE'] = self.db.name
        self.app.config['IMPORT_DIRECTORIES'] = [self.root]
        self.ctx = self.app.app_context()
        self.ctx.push()
        import_history.init_import_history_table()
        conn = sqlite3.connect(self.db.name)
        conn.execute('CREATE TABLE series (id INTEGER PRIMARY KEY, title TEXT)')
        conn.execute("INSERT INTO series VALUES (10, 'NOOB')")
        conn.execute('CREATE TABLE active_downloads (id INTEGER PRIMARY KEY, status TEXT, is_pack INTEGER, '
                     'series_id INTEGER, client_item_name TEXT, title TEXT, completed_at TEXT)')
        conn.execute("INSERT INTO active_downloads VALUES (1, 'completed', 1, 10, '00   NOOB - Integrale', 'NOOB - Integrale', NULL)")
        conn.execute("INSERT INTO active_downloads VALUES (2, 'completed', 0, 10, 'single.cbz', 'single.cbz', NULL)")
        conn.commit()
        conn.close()
        self.folder = os.path.join(self.root, '00   NOOB - Integrale')
        os.mkdir(self.folder)
        routes._pack_transfer_activity.clear()

    def tearDown(self):
        routes._pack_transfer_activity.clear()
        self.ctx.pop()
        self.db.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def status(self, download_id=1):
        conn = sqlite3.connect(self.db.name)
        row = conn.execute('SELECT status FROM active_downloads WHERE id = ?', (download_id,)).fetchone()
        conn.close()
        return row[0]

    def touch(self, name):
        path = os.path.join(self.folder, name)
        open(path, 'w').close()
        return path

    def finalize(self, path, action='skipped'):
        import_history.log_import_file(
            'op1', os.path.basename(path), path, '', 'NOOB', action, 'success', 'Doublon ignoré', None)

    def test_only_already_processed_leftovers_close_the_pack(self):
        for name in ('T03.cbr', 'T04.cbr'):
            self.finalize(self.touch(name))
        self.assertEqual(downloader.reconcile_completed_packs(), 1)
        self.assertEqual(self.status(), 'imported')

    def test_pack_with_an_unprocessed_file_stays_open(self):
        self.finalize(self.touch('T03.cbr'))
        self.touch('T01.cbr')
        self.assertEqual(downloader.reconcile_completed_packs(), 0)
        self.assertEqual(self.status(), 'completed')

    def test_pack_still_receiving_files_stays_open(self):
        self.finalize(self.touch('T03.cbr'))
        routes.note_pack_transfer_activity(self.root, os.path.join(self.folder, 'T03.cbr'))
        self.assertEqual(downloader.reconcile_completed_packs(), 0)
        self.assertEqual(self.status(), 'completed')

    def test_transfer_temp_file_keeps_pack_open(self):
        self.finalize(self.touch('T03.cbr'))
        self.touch('.syncthing.T05.cbr.tmp')
        self.assertEqual(downloader.reconcile_completed_packs(), 0)

    def test_missing_folder_is_never_closed(self):
        shutil.rmtree(self.folder)
        self.assertEqual(downloader.reconcile_completed_packs(), 0)
        self.assertEqual(self.status(), 'completed')

    def test_single_file_downloads_are_not_touched_by_pack_reconcile(self):
        downloader.reconcile_completed_packs()
        self.assertEqual(self.status(2), 'completed')

    def test_helper_ignores_unsupported_files_and_finalized_ones(self):
        self.touch('cover.jpg')
        self.finalize(self.touch('T03.cbr'))
        self.assertFalse(routes._pack_has_unfinalized_files(self.folder))
        self.touch('T09.cbz')
        self.assertTrue(routes._pack_has_unfinalized_files(self.folder))


if __name__ == '__main__':
    unittest.main()
