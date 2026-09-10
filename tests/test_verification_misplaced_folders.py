import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

WORKTREE = Path('[REDACTED_PATH]')
sys.path.insert(0, str(WORKTREE))


class VerificationMisplacedFoldersTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.library = Path(self.tmp.name) / 'BD'
        self.library.mkdir()
        self.config = {'series_template': '<univers>/<series>'}
        self.routes = importlib.import_module('blueprints.settings.routes')

    def tearDown(self):
        self.tmp.cleanup()

    def test_lists_universe_folder_mismatch_with_safe_expected_destination(self):
        current = self.library / 'Kriss de Valnor'
        current.mkdir()
        series = {
            'id': 1206, 'title': 'Kriss de Valnor', 'path': str(current),
            'library_path': str(self.library), 'universe_name': 'Thorgal',
        }
        with patch.object(self.routes, 'load_rename_config', return_value=self.config):
            result = self.routes._verify_misplaced_series_folders([series])
        self.assertEqual([{
            'series_id': 1206,
            'series_title': 'Kriss de Valnor',
            'current_path': str(current),
            'expected_path': str(self.library / 'Thorgal' / 'Kriss de Valnor'),
            'can_reconcile': True,
            'reason': 'Dossier hors de l’univers ou du template configuré',
        }], result)

    def test_reports_existing_destination_as_not_actionable(self):
        current = self.library / 'Kriss de Valnor'
        expected = self.library / 'Thorgal' / 'Kriss de Valnor'
        current.mkdir()
        expected.mkdir(parents=True)
        series = {
            'id': 1206, 'title': 'Kriss de Valnor', 'path': str(current),
            'library_path': str(self.library), 'universe_name': 'Thorgal',
        }
        with patch.object(self.routes, 'load_rename_config', return_value=self.config):
            result = self.routes._verify_misplaced_series_folders([series])
        self.assertFalse(result[0]['can_reconcile'])
        self.assertEqual('Un dossier existe déjà à la destination attendue', result[0]['reason'])


if __name__ == '__main__':
    unittest.main()
