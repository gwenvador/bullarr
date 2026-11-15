import os
import tempfile
import unittest

from config import Config


class ManualVolumeSurvivesRescanTest(unittest.TestCase):
    """« ça n'a pas conservé le volume après refresh » : le tome choisi à la main
    (destination_json.volume_override) doit remplacer celui relu dans le nom du fichier."""

    def setUp(self):
        os.makedirs('data', exist_ok=True)
        fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        Config._init_database(self.db_path)
        from app import create_app
        self.app = create_app()
        self.app.config.update(DATABASE=self.db_path, TESTING=True)
        self.root = tempfile.mkdtemp()
        self.path = os.path.join(self.root, 'El Diablo.cbz')
        with open(self.path, 'wb') as handle:
            handle.write(b'PK\x05\x06' + b'\x00' * 18)

    def tearDown(self):
        os.remove(self.db_path)
        os.remove(self.path)
        os.rmdir(self.root)

    def test_volume_override_is_applied_to_parsed(self):
        from blueprints.library.routes import _append_scanned_file
        from blueprints.library.scanner import LibraryScanner
        destination = {'library_id': 1, 'series_id': 860, 'series_title': 'Marsupilami', 'manual_override': True,
                       'volume_override': {'volume': None, 'is_integral': False, 'integral_number': None,
                                           'is_hs': True, 'hs_number': 2024, 'is_episode': False,
                                           'episode_number': None, 'is_special': False, 'special_label': None}}
        files = []
        with self.app.test_request_context('/'):
            _append_scanned_file(self.path, self.root, 'El Diablo.cbz',
                                 {'series_id': 1483, 'series_title': 'El Diablo', 'tracking_id': 5153, 'client': 'qbittorrent'},
                                 LibraryScanner(self.db_path),
                                 {self.path}, {}, files, validate_file=False,
                                 manual_destinations={self.path: destination})
        self.assertEqual(len(files), 1)
        parsed = files[0]['parsed']
        self.assertTrue(parsed['is_hs'])
        self.assertEqual(parsed['hs_number'], 2024)
        self.assertIsNone(parsed['volume'])
        # La série choisie à la main gagne, le lien vers le téléchargement suivi reste.
        self.assertEqual(files[0]['destination']['series_id'], 860)
        self.assertEqual(files[0]['destination']['tracking_id'], 5153)


if __name__ == '__main__':
    unittest.main()
