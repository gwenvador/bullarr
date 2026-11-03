"""Fichiers rattachés à un téléchargement suivi par le seul nom de leur dossier (/import).

- Le type suivi (HS, intégrale, épisode, « Remplacer quand même ») doit être conservé,
  comme sur les autres chemins de découverte.
- Un fichier que l'import automatique ne sait pas rattacher (aucun indice dans son nom,
  client autre que qBittorrent) ne doit pas s'afficher « Import automatique en attente » :
  il est signalé comme import manuel à valider.
"""
import os
import tempfile
import unittest
from unittest import mock

from flask import Flask

from blueprints.library import routes

FOLDER = 'Les.Rugbymen.Integrale.FR.CBZ-NoTag'


def _download(**overrides):
    row = {
        'id': 77, 'title': FOLDER, 'series_id': 12, 'volume_id': None, 'volume_number': None,
        'is_pack': True, 'expected_volume_count': None, 'client': 'rtorrent',
        'client_item_id': None, 'client_item_name': None, 'force_replace': True,
        'download_status': 'completed', 'is_integral': False, 'integral_number': None,
        'is_hs': True, 'hs_number': 2, 'is_episode': False, 'episode_number': None,
    }
    row.update(overrides)
    return row


def _fake_destination(series_id, volume_number, tracking_id, **flags):
    return {
        'series_id': series_id, 'library_id': 1, 'library_path': '/BD', 'library_name': 'BD',
        'series_title': 'Les Rugbymen', 'client': None, 'is_new_series': False,
        'is_oneshot': False, 'is_single_album': False, 'volume_number': volume_number,
        'tracking_id': tracking_id, 'download_status': 'completed',
        'force_replace': flags.get('force_replace', False),
        'is_integral': flags.get('is_integral', False), 'integral_number': flags.get('integral_number'),
        'is_hs': flags.get('is_hs', False), 'hs_number': flags.get('hs_number'),
        'is_episode': flags.get('is_episode', False), 'episode_number': flags.get('episode_number'),
    }


class FolderNameMatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(self.tmp.name)
        self.app = Flask(__name__)
        self.app.config['IMPORT_DIRECTORIES'] = [self.root]
        self.app.config['DATABASE'] = os.path.join(self.root, 'x.db')

    def _write(self, folder, name):
        path = os.path.join(self.root, folder, name) if folder else os.path.join(self.root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as handle:
            handle.write(b'PK\x03\x04' + b'\x00' * 64)
        return path

    def scan(self, download):
        with self.app.app_context(), \
                mock.patch.object(routes, 'load_library_import_config', return_value={'auto_import_enabled': True}), \
                mock.patch('blueprints.missing_monitor.downloader.get_trackable_active_downloads', return_value=[download]), \
                mock.patch('blueprints.missing_monitor.downloader.prepare_trackable_downloads_for_matching', side_effect=lambda rows: rows), \
                mock.patch('blueprints.missing_monitor.downloader.get_recorded_download_files', return_value={}), \
                mock.patch('blueprints.library.import_history.get_manual_override_filepaths', return_value=set()), \
                mock.patch('blueprints.library.import_history.get_manual_override_destinations', return_value={}), \
                mock.patch('blueprints.library.import_history.get_finalized_import_source_paths', return_value=set()), \
                mock.patch('blueprints.library.import_history.get_finalized_import_source_times', return_value={}), \
                mock.patch('blueprints.library.import_history.persist_discovered_import_items'), \
                mock.patch.object(routes, '_build_active_download_destination', side_effect=_fake_destination), \
                mock.patch.object(routes, '_existing_volume_info', return_value=None), \
                mock.patch.object(routes, '_is_container_archive', return_value=False), \
                mock.patch.object(routes, '_unknown_volume_hold_reason', return_value=None), \
                mock.patch.object(routes, '_repeated_failure_skip_reason', return_value=None), \
                mock.patch.object(routes, 'refine_volume_from_album_title'):
            files, _ = routes._scan_tracked_import_files(validate_files=False)
        return {item['filename']: item for item in files}

    def test_tracked_type_and_force_replace_survive_folder_match(self):
        self._write(FOLDER, 'Album.cbz')
        files = self.scan(_download())
        destination = files['Album.cbz']['destination']
        self.assertTrue(destination['is_hs'])
        self.assertEqual(destination['hs_number'], 2)
        self.assertTrue(destination['force_replace'])
        # Le nom du fichier ne dit rien : le type suivi complète le tome.
        self.assertTrue(files['Album.cbz']['parsed'].get('is_hs'))

    def test_generic_file_in_non_qbittorrent_folder_is_manual(self):
        self._write(FOLDER, 'Tome 01.cbz')
        files = self.scan(_download(is_hs=False, hs_number=None, force_replace=False, volume_number=1))
        self.assertEqual(files['Tome 01.cbz']['auto_import_skip_reason'], routes._FOLDER_NAME_ONLY_HOLD_REASON)

    def test_qbittorrent_folder_named_like_the_torrent_stays_automatic(self):
        self._write(FOLDER, 'Tome 01.cbz')
        files = self.scan(_download(is_hs=False, hs_number=None, force_replace=False, volume_number=1,
                                    client='qbittorrent', client_item_id='abc', client_item_name=FOLDER))
        self.assertIsNone(files['Tome 01.cbz']['auto_import_skip_reason'])

    def test_qbittorrent_duplicate_folder_suffix_is_manual(self):
        # « Nom.2 » : /import le rattache, le planificateur compare le nom exact du torrent.
        self._write(FOLDER + '.2', 'Tome 01.cbz')
        files = self.scan(_download(is_hs=False, hs_number=None, force_replace=False, volume_number=1,
                                    client='qbittorrent', client_item_id='abc', client_item_name=FOLDER))
        self.assertEqual(files['Tome 01.cbz']['auto_import_skip_reason'], routes._FOLDER_NAME_ONLY_HOLD_REASON)

    def test_file_whose_name_matches_the_tracked_title_stays_automatic(self):
        self._write(FOLDER, FOLDER + '.cbz')
        files = self.scan(_download(is_hs=False, hs_number=None, force_replace=False, volume_number=1, is_pack=False))
        self.assertIsNone(files[FOLDER + '.cbz']['auto_import_skip_reason'])


class UniqueImportTargetPathTest(unittest.TestCase):
    def test_never_returns_an_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(routes._unique_import_target_path(tmp, 'T01.cbz'), os.path.join(tmp, 'T01.cbz'))
            open(os.path.join(tmp, 'T01.cbz'), 'wb').close()
            open(os.path.join(tmp, 'T01_1.cbz'), 'wb').close()
            self.assertEqual(routes._unique_import_target_path(tmp, 'T01.cbz'), os.path.join(tmp, 'T01_2.cbz'))

    def test_replacement_branch_uses_a_free_target(self):
        # La branche « remplacement » de _execute_import_batch doit, comme l'import normal,
        # choisir un nom libre une fois l'ancien fichier mis en backup.
        import inspect
        source = inspect.getsource(routes._execute_import_batch)
        replace_branch = source[source.index("DELETE FROM volumes WHERE filepath = ?"):source.index("action_taken = 'replaced'")]
        self.assertIn('_unique_import_target_path(target_dir', replace_branch)


if __name__ == '__main__':
    unittest.main()
