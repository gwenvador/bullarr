import os
import sqlite3
import tempfile
import time
import unittest

from flask import Flask

from blueprints.library import import_history


class ImportClaimsAndOverridesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, 'bullarr.db')
        self.app = Flask(__name__)
        self.app.config['DATABASE'] = self.db
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.addCleanup(self.ctx.pop)
        import_history.init_import_history_table()
        import_history.init_import_manual_overrides_table()
        import_history.init_import_in_progress_table()

    def make_file(self, name):
        path = os.path.join(self.tmp.name, name)
        open(path, 'w').close()
        return path

    def query(self, sql, *args):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    # --- file claims between concurrent import operations
    def test_a_path_can_only_be_claimed_by_one_operation_at_a_time(self):
        a, b = self.make_file('a.cbz'), self.make_file('b.cbz')
        self.assertEqual(import_history.claim_import_files([a, b], 'op1'), {a, b})
        self.assertEqual(import_history.claim_import_files([a, b], 'op2'), set())
        self.assertEqual(import_history.get_in_progress_filepaths(), {a, b})

    def test_a_second_operation_only_gets_the_free_paths(self):
        a, b = self.make_file('a.cbz'), self.make_file('b.cbz')
        import_history.claim_import_files([a], 'op1')
        self.assertEqual(import_history.claim_import_files([a, b], 'op2'), {b})

    def test_releasing_an_operation_frees_only_its_paths(self):
        a, b = self.make_file('a.cbz'), self.make_file('b.cbz')
        import_history.claim_import_files([a], 'op1')
        import_history.claim_import_files([b], 'op2')
        import_history.release_import_files('op1')
        self.assertEqual(import_history.get_in_progress_filepaths(), {b})
        self.assertEqual(import_history.claim_import_files([a], 'op3'), {a})

    def test_claiming_nothing_is_a_noop(self):
        self.assertEqual(import_history.claim_import_files([], 'op1'), set())

    def test_stale_claims_of_a_crashed_operation_are_purged(self):
        a = self.make_file('a.cbz')
        import_history.claim_import_files([a], 'crashed')
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE import_in_progress SET claimed_at = datetime('now', '-1 day')")
        conn.commit()
        conn.close()
        self.assertEqual(import_history.get_in_progress_filepaths(), set())
        self.assertEqual(import_history.claim_import_files([a], 'op2'), {a})

    # --- manual assignment flag
    def test_manual_assignment_is_remembered_with_its_destination(self):
        path = self.make_file('manual.cbz')
        self.assertTrue(import_history.mark_import_file_manual(path, {'series_id': 7, 'volume': 3}))
        self.assertTrue(import_history.mark_import_file_manual(path))          # idempotent, garde la destination
        self.assertEqual(import_history.get_manual_override_filepaths(), {path})
        self.assertEqual(import_history.get_manual_override_destinations(), {path: {'series_id': 7, 'volume': 3}})
        import_history.mark_import_file_manual(path, {'series_id': 8})
        self.assertEqual(import_history.get_manual_override_destinations()[path], {'series_id': 8})

    def test_manual_flags_of_vanished_or_imported_files_are_cleaned_up(self):
        kept = self.make_file('kept.cbz')
        gone = os.path.join(self.tmp.name, 'gone.cbz')
        imported = self.make_file('imported.cbz')
        for path in (kept, gone, imported):
            import_history.mark_import_file_manual(path)
        import_history.log_import_file('op', 'imported.cbz', imported, '', '', 'imported', 'success', '', None)
        self.assertEqual(import_history.get_manual_override_filepaths(), {kept})
        self.assertEqual(self.query('SELECT filepath FROM import_manual_overrides'), [(kept,)])

    # --- re-downloaded file at an already imported path
    def test_only_a_later_download_makes_an_imported_path_new_again(self):
        from blueprints.library import routes
        path = self.make_file("Thorgal T01.cbz")
        import_history.log_import_file("op", "Thorgal T01.cbz", path, "/BD/x.cbz", "", "imported", "success", "", None)
        times = import_history.get_finalized_import_source_times()
        self.assertIn(path, times)
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE IF NOT EXISTS active_downloads (id INTEGER PRIMARY KEY, created_at TEXT)")
        conn.execute("INSERT INTO active_downloads VALUES (1, datetime('now', '-1 day'))")   # avant l'import
        conn.execute("INSERT INTO active_downloads VALUES (2, datetime('now', '+1 hour'))")  # retéléchargement
        conn.commit()
        conn.close()
        item = lambda tid: {"filepath": path, "destination": {"tracking_id": tid}}
        self.assertEqual(routes._exclude_finalized_import_files([item(1)], times), [])          # copie revenue: écartée
        self.assertEqual(len(routes._exclude_finalized_import_files([item(2)], times)), 1)     # retéléchargé: repris
        self.assertEqual(len(routes._exclude_finalized_import_files([item(None)], times)), 0)
        pending = [{"title": "Thorgal T01.cbz", "created_at": "2000-01-01 00:00:00"},
                   {"title": "Thorgal T01.cbz", "created_at": "2999-01-01 00:00:00"}]
        kept = routes._exclude_finalized_pending_downloads(pending, times)
        self.assertEqual([p["created_at"] for p in kept], ["2999-01-01 00:00:00"])

    # --- stale history rows
    def test_file_rows_stuck_in_processing_for_a_day_are_closed_at_startup(self):
        # Ligne sans opération parente (cas réel du 1er août): la purge des orphelines, qui
        # passe par l opération parente, ne la voit jamais. Seule l ancienneté la rattrape.
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO import_history_files (operation_id, filename, source_path, destination_path, series_title, action, status, message, created_at) "
                     "VALUES ('old-op', 'a.cbz', '/x/a.cbz', '', '', 'processing', 'processing', '', datetime('now', '-3 days'))")
        conn.execute("INSERT INTO import_history_files (operation_id, filename, source_path, destination_path, series_title, action, status, message, created_at) "
                     "VALUES ('old-op', 'b.cbz', '/x/b.cbz', '', '', 'processing', 'processing', '', datetime('now'))")
        conn.commit()
        conn.close()
        import_history.cleanup_stale_operations()
        rows = dict(self.query('SELECT filename, status FROM import_history_files'))
        self.assertEqual(rows, {'a.cbz': 'error', 'b.cbz': 'processing'})

    def test_startup_releases_claims_and_downloads_left_by_an_interrupted_import(self):
        a = self.make_file('a.cbz')
        import_history.claim_import_files([a], 'interrupted-op')
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE IF NOT EXISTS active_downloads (id INTEGER PRIMARY KEY, status TEXT)")
        conn.execute("INSERT INTO active_downloads (id, status) VALUES (1, 'importing'), (2, 'pending')")
        conn.commit()
        conn.close()
        import_history.cleanup_stale_operations()
        self.assertEqual(import_history.get_in_progress_filepaths(), set())
        self.assertEqual(self.query('SELECT id, status FROM active_downloads ORDER BY id'), [(1, 'completed'), (2, 'pending')])

    # --- small helpers
    def test_delete_import_operation_removes_only_that_operation(self):
        import_history.log_import_operation('op-a', 'auto_import', '/x', 'completed')
        import_history.log_import_operation('op-b', 'auto_import', '/x', 'completed')
        import_history.delete_import_operation('op-a')
        self.assertEqual(self.query('SELECT operation_id FROM import_history'), [('op-b',)])


class DiscoveredItemsPersistenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, 'bullarr.db')
        self.root = os.path.realpath(os.path.join(self.tmp.name, 'imports'))
        os.makedirs(self.root)
        self.app = Flask(__name__)
        self.app.config['DATABASE'] = self.db
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.addCleanup(self.ctx.pop)

    def item(self, name, tracking_id=1):
        return {'filepath': os.path.join(self.root, name), 'filename': name, 'destination': {'tracking_id': tracking_id}}

    def rows(self):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute('SELECT source_path, source_available, state FROM import_items ORDER BY source_path').fetchall()
        finally:
            conn.close()

    def test_items_are_stored_and_files_that_disappeared_become_unavailable(self):
        import_history.persist_discovered_import_items([self.item('a.cbz'), self.item('b.cbz')], scanned_roots=[self.root])
        self.assertEqual([r[1] for r in self.rows()], [1, 1])
        import_history.persist_discovered_import_items([self.item('a.cbz')], scanned_roots=[self.root])
        rows = dict((os.path.basename(path), available) for path, available, _ in self.rows())
        self.assertEqual(rows, {'a.cbz': 1, 'b.cbz': 0})

    def test_untracked_items_and_unscanned_roots_are_left_alone(self):
        other = os.path.realpath(os.path.join(self.tmp.name, 'other'))
        import_history.persist_discovered_import_items([self.item('x.cbz', tracking_id=3)], scanned_roots=[self.root])
        import_history.persist_discovered_import_items(
            [{'filepath': os.path.join(self.root, 'no-tracking.cbz'), 'filename': 'no-tracking.cbz'}], scanned_roots=[other])
        self.assertEqual([os.path.basename(r[0]) for r in self.rows()], ['x.cbz'])
        self.assertEqual(self.rows()[0][1], 1)

    def test_one_file_name_with_invalid_bytes_does_not_lose_the_whole_batch(self):
        bad = 'Thorgal - 07 - L\'enfant des \udce9toiles.cbz'            # octets Latin-1 d'un export aMule
        import_history.persist_discovered_import_items(
            [self.item('first.cbz'), self.item(bad), self.item('last.cbz')], scanned_roots=[self.root])
        names = [os.path.basename(path) for path, _, _ in self.rows()]
        self.assertEqual(names, ['first.cbz', 'last.cbz'])

    def test_purge_removes_only_unavailable_files_of_finished_or_vanished_downloads(self):
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE active_downloads (id INTEGER PRIMARY KEY, status TEXT)")
        conn.executemany("INSERT INTO active_downloads VALUES (?, ?)",
                         [(1, 'imported'), (2, 'skipped'), (3, 'cancelled'), (4, 'completed'), (5, 'failed'), (6, 'pending')])
        conn.commit()
        conn.close()
        items = [self.item(f'{n}.cbz', tracking_id=n) for n in (1, 2, 3, 4, 5, 6, 7)]   # 7: téléchargement disparu
        import_history.persist_discovered_import_items(items, scanned_roots=[self.root])
        import_history.persist_discovered_import_items([self.item('1-on-disk.cbz', tracking_id=1)], scanned_roots=[self.root])
        self.assertEqual(import_history.purge_finished_import_items(), 4)            # 1, 2, 3 et 7, absents du disque
        remaining = sorted(os.path.basename(path) for path, _, _ in self.rows())
        self.assertEqual(remaining, ['1-on-disk.cbz', '4.cbz', '5.cbz', '6.cbz'])
        self.assertEqual(import_history.purge_finished_import_items(), 0)

    def test_unavailable_marker_targets_the_resolved_path(self):
        import_history.persist_discovered_import_items([self.item('a.cbz')], scanned_roots=[])
        import_history.mark_import_item_unavailable(os.path.join(self.root, 'a.cbz'))
        import_history.mark_import_item_unavailable('')
        self.assertEqual(self.rows()[0][1], 0)


if __name__ == '__main__':
    unittest.main()
