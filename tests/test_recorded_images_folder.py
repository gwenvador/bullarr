import os
import tempfile
import unittest
from unittest import mock

from flask import Flask

from blueprints.library import routes

NAME = 'Tomka.le.gitan.de.Guernica.Carlotto.Palumbo.FR.[JPG]-NOTAG'


class RecordedImagesFolderTest(unittest.TestCase):
    """Cas Tomka: le client donne la liste exacte des fichiers (166 .jpg), aucun n'est importable."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(self.tmp.name)
        folder = os.path.join(self.root, NAME)
        os.makedirs(folder)
        self.names = [f'Tomka-{i:03d}.jpg' for i in range(1, 8)]
        for name in self.names:
            open(os.path.join(folder, name), 'wb').write(b'x')
        self.app = Flask(__name__)
        self.app.config['IMPORT_DIRECTORIES'] = [self.root]
        self.app.config['DATABASE'] = os.path.join(self.root, 'x.db')
        self.download = {'id': 4987, 'title': NAME, 'series_id': 1405, 'volume_id': None, 'volume_number': None,
                         'is_pack': True, 'expected_volume_count': None, 'client': 'qbittorrent',
                         'client_item_id': 'abc', 'client_item_name': NAME, 'force_replace': False,
                         'download_status': 'completed', 'is_integral': False, 'integral_number': None,
                         'is_hs': False, 'hs_number': None, 'is_episode': False, 'episode_number': None}

    def scan(self):
        recorded = {4987: [{'relative_path': f'{NAME}/{n}', 'size': 1} for n in self.names]}
        with self.app.app_context(), \
                mock.patch.object(routes, 'load_library_import_config', return_value={}), \
                mock.patch('blueprints.missing_monitor.downloader.get_trackable_active_downloads', return_value=[self.download]), \
                mock.patch('blueprints.missing_monitor.downloader.prepare_trackable_downloads_for_matching', side_effect=lambda rows: rows), \
                mock.patch('blueprints.missing_monitor.downloader.get_recorded_download_files', return_value=recorded), \
                mock.patch('blueprints.library.import_history.get_manual_override_filepaths', return_value=set()), \
                mock.patch('blueprints.library.import_history.get_manual_override_destinations', return_value={}), \
                mock.patch('blueprints.library.import_history.get_finalized_import_source_paths', return_value=set()), \
                mock.patch.object(routes, '_build_active_download_destination', return_value=None):
            return routes._scan_tracked_import_files(validate_files=False)

    def test_folder_of_images_is_reported_as_incompatible(self):
        files, folders = self.scan()
        self.assertEqual(files, [])
        self.assertEqual(len(folders), 1)
        self.assertEqual(folders[0]['pack_download_id'], 4987)
        self.assertEqual(folders[0]['file_count'], len(self.names))
        self.assertEqual(folders[0]['sample_extensions'], ['.jpg'])
