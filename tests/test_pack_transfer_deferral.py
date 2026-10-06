import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

from flask import Flask

from blueprints.library import routes


class PackTransferDeferralTest(unittest.TestCase):
    """A pack still being copied over the network must stay 'completed' (waiting)."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.db = tempfile.NamedTemporaryFile()
        self.app = Flask(__name__)
        self.app.config['DATABASE'] = self.db.name
        self.app.config['IMPORT_DIRECTORIES'] = [self.root]
        conn = sqlite3.connect(self.db.name)
        conn.execute('CREATE TABLE active_downloads (id INTEGER PRIMARY KEY, status TEXT, is_pack INTEGER, completed_at TEXT)')
        conn.execute("INSERT INTO active_downloads VALUES (1, 'importing', 1, NULL)")
        conn.execute("INSERT INTO active_downloads VALUES (2, 'importing', 0, NULL)")
        conn.commit()
        conn.close()
        self.pack_dir = os.path.join(self.root, 'NOOB - Integrale')
        os.mkdir(self.pack_dir)
        self.source = os.path.join(self.pack_dir, 'Noob - T05.cbr')
        routes._pack_transfer_activity.clear()
        routes._deferred_pack_finalizations.clear()
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()
        self.db.close()
        shutil.rmtree(self.root, ignore_errors=True)
        routes._pack_transfer_activity.clear()
        routes._deferred_pack_finalizations.clear()

    def status(self, tracking_id=1):
        conn = sqlite3.connect(self.db.name)
        row = conn.execute('SELECT status FROM active_downloads WHERE id = ?', (tracking_id,)).fetchone()
        conn.close()
        return row[0]

    def finish(self, tracking_id=1, outcome='skipped'):
        routes._maybe_complete_tracking_after_move(self.source, {'tracking_id': tracking_id}, outcome=outcome)

    def test_pack_with_recent_new_files_stays_waiting_after_all_present_files_are_duplicates(self):
        routes.note_pack_transfer_activity(self.root, self.source)
        self.finish()
        self.assertEqual(self.status(), 'completed')
        self.assertTrue(os.path.isdir(self.pack_dir))
        self.assertIn(1, routes._deferred_pack_finalizations)

    def test_pack_is_finalized_once_quiet(self):
        routes.note_pack_transfer_activity(self.root, self.source)
        self.finish()
        key = os.path.realpath(self.pack_dir)
        routes._pack_transfer_activity[key] -= routes.PACK_TRANSFER_QUIET_SECONDS + 1
        routes.retry_deferred_pack_finalizations()
        self.assertEqual(self.status(), 'skipped')
        self.assertNotIn(1, routes._deferred_pack_finalizations)
        self.assertFalse(os.path.exists(self.pack_dir))

    def test_temp_transfer_file_keeps_pack_waiting_without_recorded_activity(self):
        open(os.path.join(self.pack_dir, '.syncthing.Noob - T01.cbr.tmp'), 'w').close()
        self.finish()
        self.assertEqual(self.status(), 'completed')

    def test_pack_without_activity_is_finalized_immediately(self):
        self.finish()
        self.assertEqual(self.status(), 'skipped')

    def test_pack_folder_removed_by_client_still_waits_during_activity(self):
        routes.note_pack_transfer_activity(self.root, self.source)
        shutil.rmtree(self.pack_dir)
        self.finish()
        self.assertEqual(self.status(), 'completed')

    def test_single_file_download_is_never_deferred(self):
        routes.note_pack_transfer_activity(self.root, self.source)
        self.finish(tracking_id=2)
        self.assertEqual(self.status(2), 'skipped')

    def test_note_activity_ignores_files_at_import_root(self):
        routes.note_pack_transfer_activity(self.root, os.path.join(self.root, 'loose.cbz'))
        self.assertEqual(routes._pack_transfer_activity, {})


if __name__ == '__main__':
    unittest.main()
