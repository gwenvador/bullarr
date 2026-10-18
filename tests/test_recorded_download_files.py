import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flask import Flask

from blueprints.library import routes
from blueprints.library.routes import (
    index_recorded_download_files,
    path_in_recorded_download_folder,
    recorded_download_id_for_path,
)
from blueprints.missing_monitor import downloader

EXTENSIONS = {'.cbz', '.cbr', '.zip', '.rar', '.pdf'}


def _download(download_id=1, **extra):
    row = {'id': download_id, 'series_id': 10, 'volume_number': None, 'client': 'qbittorrent',
           'client_item_id': 'abc', 'download_status': 'completed', 'is_pack': False}
    row.update(extra)
    return row


class IndexRecordedDownloadFilesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, rel, size):
        path = Path(self.root) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'x' * size)
        return str(path)

    def test_files_are_found_by_recorded_path_whatever_their_names(self):
        listed = self._write('Torrent.Name.FRENCH/sans-rapport-01.cbz', 3)
        self._write('Torrent.Name.FRENCH/autre.cbz', 2)
        index = index_recorded_download_files(
            [_download()], {1: [{'relative_path': 'Torrent.Name.FRENCH/sans-rapport-01.cbz', 'size': 3}]},
            [self.root], EXTENSIONS)
        self.assertEqual(list(index['by_path']), [listed])
        self.assertTrue(index['by_path'][listed]['complete'])
        self.assertEqual(index['covered_tops'], {(self.root, 'torrent.name.french'): 1})
        self.assertFalse(index['downloads'][1]['multi'])
        self.assertFalse(index['downloads'][1]['nested'])

    def test_unknown_size_counts_as_complete_on_the_page(self):
        path = self._write('Alix - 04.cbz', 2)
        index = index_recorded_download_files(
            [_download(client='amule')], {1: [{'relative_path': 'Alix - 04.cbz', 'size': None}]},
            [self.root], EXTENSIONS)
        self.assertTrue(index['by_path'][path]['complete'])
        self.assertIsNone(index['by_path'][path]['expected_size'])

    def test_file_still_copying_is_not_complete(self):
        path = self._write('Album.cbz', 2)
        index = index_recorded_download_files(
            [_download()], {1: [{'relative_path': 'Album.cbz', 'size': 10}]}, [self.root], EXTENSIONS)
        self.assertFalse(index['by_path'][path]['complete'])

    def test_subdirectories_mark_the_download_nested(self):
        self._write('Pack/Tome 1.cbz', 1)
        self._write('Pack/Bonus/HS.cbz', 1)
        index = index_recorded_download_files(
            [_download()], {1: [{'relative_path': 'Pack/Tome 1.cbz', 'size': 1},
                                {'relative_path': 'Pack/Bonus/HS.cbz', 'size': 1}]},
            [self.root], EXTENSIONS)
        self.assertTrue(index['downloads'][1]['nested'])
        self.assertTrue(index['downloads'][1]['multi'])

    def test_extensionless_file_renamed_with_its_extension_is_found(self):
        renamed = self._write('Chagrin [PDF]-NOTAG.pdf', 4)
        index = index_recorded_download_files(
            [_download()], {1: [{'relative_path': 'Chagrin [PDF]-NOTAG', 'size': 4}]}, [self.root], EXTENSIONS)
        self.assertIn(renamed, index['by_path'])
        self.assertTrue(path_in_recorded_download_folder(index, self.root, renamed))

    def test_extracted_container_archive_folder_belongs_to_the_download(self):
        inner = self._write('Pack Complet/01.cbz', 1)
        index = index_recorded_download_files(
            [_download()], {1: [{'relative_path': 'Pack Complet.zip', 'size': 99}]}, [self.root], EXTENSIONS)
        self.assertEqual(index['downloads'][1]['extracted_dirs'], [os.path.join(self.root, 'Pack Complet')])
        self.assertEqual(recorded_download_id_for_path(index, inner), 1)
        self.assertTrue(path_in_recorded_download_folder(index, self.root, inner))

    def test_download_without_any_local_file_falls_back_to_old_lookup(self):
        index = index_recorded_download_files(
            [_download()], {1: [{'relative_path': 'Pas encore là.cbz', 'size': 1}]}, [self.root], EXTENSIONS)
        self.assertEqual(index['downloads'], {})
        self.assertEqual(index['covered_tops'], {})

    def test_path_outside_the_import_root_is_ignored(self):
        self._write('Top/ok.cbz', 1)
        index = index_recorded_download_files(
            [_download()], {1: [{'relative_path': 'Top/ok.cbz', 'size': 1},
                                {'relative_path': 'Top/../../escape.cbz', 'size': 1}]},
            [self.root], EXTENSIONS)
        self.assertEqual(len(index['by_path']), 1)

    def test_unlisted_file_in_a_recorded_folder_is_not_claimed(self):
        self._write('Top/listed.cbz', 1)
        stray = self._write('Top/stray.cbz', 1)
        index = index_recorded_download_files(
            [_download()], {1: [{'relative_path': 'Top/listed.cbz', 'size': 1}]}, [self.root], EXTENSIONS)
        self.assertIsNone(recorded_download_id_for_path(index, stray))
        self.assertTrue(path_in_recorded_download_folder(index, self.root, stray))

    def test_destination_keeps_tracked_type_and_force_replace(self):
        with mock.patch.object(routes, '_build_active_download_destination', return_value={}) as build:
            routes._destination_from_download(_download(is_integral=True, integral_number=2, force_replace=True))
        kwargs = build.call_args.kwargs
        self.assertTrue(kwargs['is_integral'])
        self.assertEqual(kwargs['integral_number'], 2)
        self.assertTrue(kwargs['force_replace'])


class RecordClientDownloadFilesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, 'test.db')
        conn = sqlite3.connect(self.db)
        conn.execute('CREATE TABLE active_downloads (id INTEGER PRIMARY KEY, client TEXT, client_item_id TEXT, title TEXT)')
        conn.execute("INSERT INTO active_downloads VALUES (1, 'qbittorrent', 'abc', 'Titre recherche'), "
                     "(2, 'amule', 'def', 'Alix - 04 - La marine antique.cbz'), (3, 'telegram', NULL, 'x')")
        conn.commit()
        conn.close()
        self.app = Flask('test')
        self.app.config['DATABASE'] = self.db
        downloader._download_files_attempts.clear()

    def tearDown(self):
        self._tmp.cleanup()

    def test_records_once_and_reads_back(self):
        files = [{'name': 'Top/a.cbz', 'size': 5}, {'name': 'Top/b.cbz', 'size': 6}]
        with self.app.app_context(), mock.patch(
                'blueprints.qbittorrent.routes.get_qbittorrent_torrent_files', return_value=files) as api:
            self.assertTrue(downloader.record_client_download_files(1))
            self.assertTrue(downloader.record_client_download_files(1))
            self.assertEqual(api.call_count, 1)
            recorded = downloader.get_recorded_download_files([1, 2])
        self.assertEqual(recorded, {1: [{'relative_path': 'Top/a.cbz', 'size': 5},
                                        {'relative_path': 'Top/b.cbz', 'size': 6}]})

    def test_amule_records_its_ed2k_file_name_without_asking_a_client(self):
        with self.app.app_context(), mock.patch(
                'blueprints.qbittorrent.routes.get_qbittorrent_torrent_files') as api:
            self.assertTrue(downloader.record_client_download_files(2))
            recorded = downloader.get_recorded_download_files([2])
        api.assert_not_called()
        self.assertEqual(recorded, {2: [{'relative_path': 'Alix - 04 - La marine antique.cbz', 'size': None}]})

    def test_telegram_records_the_file_it_wrote(self):
        with self.app.app_context():
            self.assertFalse(downloader.record_client_download_files(3))
            downloader.record_local_download_file(3, 'Album_123.cbz', 42)
            self.assertTrue(downloader.record_client_download_files(3))
            recorded = downloader.get_recorded_download_files([3])
        self.assertEqual(recorded, {3: [{'relative_path': 'Album_123.cbz', 'size': 42}]})

    def test_backfill_includes_completed_amule_downloads(self):
        rows = [{'id': 2, 'client': 'amule', 'client_item_id': None, 'download_status': 'completed'}]
        with self.app.app_context():
            downloader.record_missing_download_files(rows)
            self.assertIn(2, downloader.get_recorded_download_files([2]))

    def test_backfill_waits_before_retrying_a_failed_lookup(self):
        rows = [{'id': 1, 'client': 'qbittorrent', 'client_item_id': 'abc', 'download_status': 'completed'}]
        with self.app.app_context(), mock.patch(
                'blueprints.qbittorrent.routes.get_qbittorrent_torrent_files', return_value=None) as api:
            downloader.record_missing_download_files(rows)
            downloader.record_missing_download_files(rows)
        self.assertEqual(api.call_count, 1)

    def test_backfill_records_pending_amule_downloads(self):
        rows = [{'id': 2, 'client': 'amule', 'client_item_id': None, 'download_status': 'pending'}]
        with self.app.app_context():
            downloader.record_missing_download_files(rows)
            self.assertIn(2, downloader.get_recorded_download_files([2]))

    def test_backfill_ignores_downloads_still_in_progress(self):
        rows = [{'id': 1, 'client': 'qbittorrent', 'client_item_id': 'abc', 'download_status': 'pending'}]
        with self.app.app_context(), mock.patch(
                'blueprints.qbittorrent.routes.get_qbittorrent_torrent_files') as api:
            downloader.record_missing_download_files(rows)
        api.assert_not_called()


if __name__ == '__main__':
    unittest.main()
