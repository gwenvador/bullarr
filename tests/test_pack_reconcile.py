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
                     'series_id INTEGER, client_item_name TEXT, title TEXT, completed_at TEXT, '
                     'client_item_id TEXT, created_at TEXT)')
        conn.execute("INSERT INTO active_downloads (id, status, is_pack, series_id, client_item_name, title, completed_at) VALUES (1, 'completed', 1, 10, '00   NOOB - Integrale', 'NOOB - Integrale', NULL)")
        conn.execute("INSERT INTO active_downloads (id, status, is_pack, series_id, client_item_name, title, completed_at) VALUES (2, 'completed', 0, 10, 'single.cbz', 'single.cbz', NULL)")
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

    def test_missing_folder_without_any_import_is_not_closed(self):
        shutil.rmtree(self.folder)
        self.assertEqual(downloader.reconcile_completed_packs(), 0)
        self.assertEqual(self.status(), 'completed')

    def test_folder_removed_after_its_files_were_imported_closes_the_pack(self):
        # Cas « Les Innommables »: tous les tomes importés, puis le torrent retiré du client
        # (le dossier disparaît). Le pack ne doit pas rester « Prêt - scan automatique sous peu ».
        self.finalize(self.touch('T01.cbz'), action='imported')
        shutil.rmtree(self.folder)
        self.assertEqual(downloader.reconcile_completed_packs(), 1)
        self.assertEqual(self.status(), 'imported')

    def test_an_import_from_another_folder_does_not_close_the_pack(self):
        other = os.path.join(self.root, 'NOOB - Integrale bis')
        os.mkdir(other)
        self.finalize(os.path.join(other, 'T01.cbz'), action='imported')
        shutil.rmtree(self.folder)
        self.assertEqual(downloader.reconcile_completed_packs(), 0)
        self.assertEqual(self.status(), 'completed')

    def test_archive_pack_closes_once_the_archive_has_a_result(self):
        # Pack livré en archive (cas « L'âge de Bronze ») : validé depuis /import.
        conn = sqlite3.connect(self.db.name)
        conn.execute("UPDATE active_downloads SET client_item_name = NULL, title = 'Bronze.BDPACK.zip' WHERE id = 1")
        conn.commit()
        conn.close()
        shutil.rmtree(self.folder)
        archive = os.path.join(self.root, 'Bronze.BDPACK.zip')
        open(archive, 'w').close()
        self.assertEqual(downloader.reconcile_completed_packs(), 0)
        self.finalize(archive)
        self.assertEqual(downloader.reconcile_completed_packs(), 1)
        self.assertEqual(self.status(), 'imported')

    def test_pack_is_finished_drives_the_awaiting_closure_flag(self):
        roots = [self.root]
        self.touch('T01.cbz')
        self.assertFalse(downloader.pack_is_finished('00   NOOB - Integrale', self.db.name, roots))
        self.finalize(os.path.join(self.folder, 'T01.cbz'), action='imported')
        self.assertTrue(downloader.pack_is_finished('00   NOOB - Integrale', self.db.name, roots))
        self.assertTrue(downloader._pack_awaiting_closure(1, self.db.name))
        self.assertFalse(downloader.pack_is_finished('../etc', self.db.name, roots))

    def test_folder_emptied_by_the_import_closes_right_away(self):
        imported = os.path.join(self.folder, 'T01.cbz')
        self.finalize(imported, action='imported')                         # importé puis déplacé: dossier vide
        routes.note_pack_transfer_activity(self.root, imported)            # activité toute récente
        self.assertEqual(downloader.reconcile_completed_packs(), 1)
        self.assertEqual(self.status(), 'imported')

    def test_pack_waits_for_the_file_count_announced_by_the_torrent(self):
        conn = sqlite3.connect(self.db.name)
        conn.execute("UPDATE active_downloads SET client_item_id = 'abc', created_at = '2000-01-01 00:00:00' WHERE id = 1")
        conn.commit()
        conn.close()
        with mock.patch("blueprints.qbittorrent.routes.get_qbittorrent_torrent_file_count", return_value=3):
            self.finalize(os.path.join(self.folder, "T01.cbz"), action="imported")  # 1 fichier sur 3 arrivé et importé
            self.assertEqual(downloader.reconcile_completed_packs(), 0)
            self.assertEqual(self.status(), "completed")
            self.finalize(os.path.join(self.folder, "T02.cbz"), action="imported")
            self.finalize(os.path.join(self.folder, "T03.cbz"), action="skipped")
            self.assertEqual(downloader.reconcile_completed_packs(), 1)
            self.assertEqual(self.status(), "imported")

    def test_empty_folder_without_any_import_waits(self):
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


class LocalCopyStateTest(unittest.TestCase):
    """Cas Blake et Mortimer T01-T31: terminé sur la seedbox, fichiers encore en copie."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.app = Flask(__name__)
        self.app.config["IMPORT_DIRECTORIES"] = [self.root]
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.addCleanup(self.ctx.pop)
        routes._pack_transfer_activity.clear()
        self.addCleanup(routes._pack_transfer_activity.clear)
        self.name = "Blake et Mortimer - T01 a 31"
        self.folder = os.path.join(self.root, self.name)

    def state(self):
        return downloader.local_copy_state(self.name, [self.root])

    def touch(self, name):
        os.makedirs(self.folder, exist_ok=True)
        open(os.path.join(self.folder, name), "w").close()

    def test_absent_then_copying_then_settling_then_present(self):
        self.assertEqual(self.state(), {"state": "absent", "files": 0})
        self.touch("T01.cbz")
        self.touch(".syncthing.T02.cbz.tmp")
        self.assertEqual(self.state(), {"state": "copying", "files": 1})
        os.rename(os.path.join(self.folder, ".syncthing.T02.cbz.tmp"), os.path.join(self.folder, "T02.cbz"))
        routes.note_pack_transfer_activity(self.root, os.path.join(self.folder, "T02.cbz"))
        self.assertEqual(self.state(), {"state": "settling", "files": 2})
        routes._pack_transfer_activity.clear()
        self.assertEqual(self.state(), {"state": "present", "files": 2})

    def test_single_file_download_and_unsafe_names(self):
        open(os.path.join(self.root, "Album.cbz"), "w").close()
        self.assertEqual(downloader.local_copy_state("Album.cbz", [self.root]), {"state": "present", "files": 1})
        self.assertEqual(downloader.local_copy_state("../etc", [self.root])["state"], "absent")

    def test_emptied_folder_is_not_shown_as_a_copy(self):
        os.makedirs(self.folder)
        self.assertEqual(self.state(), {"state": "empty", "files": 0})

    def test_quiet_period_is_two_minutes(self):
        self.assertEqual(routes.PACK_TRANSFER_QUIET_SECONDS, 120)
